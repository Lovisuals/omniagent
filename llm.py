import json, time, logging, re, inspect, os
from typing import Any, Callable, get_type_hints
import requests
import brain
from config import (
    LLM_KEY, LLM_URL, MODEL, FALLBACK_MODEL,
    MAX_LOOPS, MAX_OUT, HTTP_T,
    CONSOLIDATE_EVERY, DECAY_EVERY,
)
try:
    from run_agent import AIAgent
    from model_tools import registry
    HERMES_AVAILABLE = True
except ImportError:
    HERMES_AVAILABLE = False
log = logging.getLogger("llm")
TOOLS: dict[str, dict] = {}
def _py_to_json(t: Any) -> dict:
    m = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    return {"type": m.get(t, "string")}
def tool(desc: str):
    def deco(fn: Callable) -> Callable:
        hints, sig = get_type_hints(fn), inspect.signature(fn)
        props, required = {}, []
        for name, p in sig.parameters.items():
            props[name] = _py_to_json(hints.get(name, str))
            if p.default is inspect.Parameter.empty: required.append(name)
        schema = {"type": "object", "properties": props, "required": required}
        TOOLS[fn.__name__] = {"fn": fn, "description": desc, "schema": schema}
        if HERMES_AVAILABLE:
            def hermes_handler(args, **kwargs): return fn(**args)
            registry.register(name=fn.__name__, toolset="omni", schema={"description": desc, "parameters": schema}, handler=hermes_handler, description=desc, emoji="🤖")
        return fn
    return deco
_FUNC_TEXT_RE = re.compile(r"<function=(?P<name>\w+)>(?P<args>\{.*?\})(?:</function>)?", re.DOTALL)
def _salvage(content: str) -> list[dict]:
    out = []
    for i, m in enumerate(_FUNC_TEXT_RE.finditer(content or "")):
        name, raw = m.group("name"), m.group("args")
        try: json.loads(raw)
        except: continue
        out.append({"id": f"salvaged_{i}", "type": "function", "function": {"name": name, "arguments": raw}})
    return out
def _llm_call(messages: list, tool_defs: list, model: str, max_retries: int = 3) -> tuple[int, dict | str]:
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(LLM_URL, headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"}, json={"model": model, "messages": messages, "tools": tool_defs, "tool_choice": "auto", "temperature": 0.2}, timeout=HTTP_T)
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After", 12)) * (2 ** attempt), 35)
                time.sleep(wait); continue
            if r.status_code == 400 and "model_decommissioned" in r.text.lower(): return 400, "model_decommissioned"
            return (200, r.json()) if r.status_code == 200 else (r.status_code, r.text[:800])
        except requests.RequestException as e: return 0, str(e)
    return 429, "Rate limit persisted"
_SYSTEM_CORE = ("You are OmniAgent v3 — a rare intelligence with persistent long-term memory, self-reflection, and self-evolution.\n\n"
                "CAPABILITIES:\n1. ASSISTANT — any domain. Use web_search / fetch_url when needed.\n2. MEMORY AGENT — call remember() on every important fact. Call recall_tool() proactively when relevant.\n3. SELF-CRITIC — call reflect() when useful.\n4. SELF-DEVELOPER — read_self → write_self (COMPLETE file only, no diffs).\n\n"
                "GROUNDING RULE: Facts not in GROUNDED MEMORY and not from a tool are UNKNOWN. Never fabricate. Offer to search instead.\n\n"
                "After successful self-modification: say 'Done — /reload to activate.'\nNever remove OWNER_ID guard, boot-test, or brain.load().\nTool calls: native tool_calls JSON only.")
def llm_agent(user_msg: str, user_id: str = "") -> str:
    m = brain.meta(); m["interact_count"] = m.get("interact_count", 0) + 1
    recent = m.setdefault("recent_interactions", []); recent.append(user_msg)
    if len(recent) > 20: m["recent_interactions"] = recent[-20:]
    m["recall_ids_this_turn"] = []
    if m["interact_count"] % DECAY_EVERY == 0 and m["interact_count"] % CONSOLIDATE_EVERY != 0: brain.decay_pass(full=False)
    if m["interact_count"] % CONSOLIDATE_EVERY == 0: brain.consolidate(do_commit=True)
    if HERMES_AVAILABLE:
        try:
            base_url, memory_ctx = LLM_URL.split("/chat/completions")[0], brain.memory_context(user_msg, ctx_filter=user_id)
            agent = AIAgent(base_url=base_url, api_key=LLM_KEY, model=MODEL, max_iterations=MAX_LOOPS, ephemeral_system_prompt=_SYSTEM_CORE + "\n\n" + memory_ctx, enabled_toolsets=["omni"], verbose_logging=False, quiet_mode=True, user_id=user_id)
            res = agent.run_conversation(user_msg)
            if res: return res
        except Exception as e: log.exception("Hermes failed: %s", e)
    system, tool_defs = _SYSTEM_CORE + brain.memory_context(user_msg, ctx_filter=user_id), [{"type": "function", "function": {"name": n, "description": td["description"], "parameters": td["schema"]}} for n, td in TOOLS.items()]
    messages, cur_model, fallback, rl_cnt = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}], MODEL, False, 0
    for _ in range(MAX_LOOPS):
        status, data = _llm_call(messages, tool_defs, cur_model)
        if status == 429:
            rl_cnt += 1
            if rl_cnt >= 2 and not fallback: cur_model, fallback = FALLBACK_MODEL, True; messages.append({"role": "user", "content": "Rate limit hit. Using fallback."}); continue
            return "⚠️ Rate limit reached."
        if status == 400 and "model_decommissioned" in str(data).lower():
            if not fallback: cur_model, fallback = FALLBACK_MODEL, True; continue
            return "⚠️ Model unavailable."
        if status == 0: return f"⚠️ network error: {data}"
        if status == 400 and not fallback and "tool_use_failed" in str(data): cur_model, fallback = FALLBACK_MODEL, True; messages.append({"role": "user", "content": "Tool format error."}); continue
        if status != 200: return f"⚠️ HTTP {status}: {str(data)[:300]}"
        msg = data["choices"][0]["message"]; content, calls = msg.get("content") or "", msg.get("tool_calls") or []
        if not calls and "<function=" in content:
            sal = _salvage(content)
            if sal: calls, content = sal, ""
        messages.append({"role": "assistant", "content": content, "tool_calls": calls or None})
        if not calls: return content or "(empty)"
        for c in calls:
            fname, args = c["function"]["name"], json.loads(c["function"].get("arguments") or "{}")
            res = TOOLS[fname]["fn"](**args) if fname in TOOLS else f"ERROR: unknown tool '{fname}'"
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": fname, "content": str(res)[:MAX_OUT]})
    return "⚠️ Loop limit reached"