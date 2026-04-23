import json, time, logging, re, inspect
from typing import Any, Callable, get_type_hints

import requests

import brain
from config import (
    LLM_KEY, LLM_URL, MODEL, FALLBACK_MODEL,
    MAX_LOOPS, MAX_OUT, HTTP_T,
    CONSOLIDATE_EVERY, DECAY_EVERY,
)

log = logging.getLogger("llm")

TOOLS: dict[str, dict] = {}


def _py_to_json(t: Any) -> dict:
    m = {str: "string", int: "integer", float: "number",
         bool: "boolean", list: "array", dict: "object"}
    return {"type": m.get(t, "string")}


def tool(desc: str):
    def deco(fn: Callable) -> Callable:
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        props, required = {}, []
        for name, p in sig.parameters.items():
            props[name] = _py_to_json(hints.get(name, str))
            if p.default is inspect.Parameter.empty:
                required.append(name)
        TOOLS[fn.__name__] = {
            "fn": fn,
            "description": desc,
            "schema": {"type": "object", "properties": props, "required": required},
        }
        return fn
    return deco


_FUNC_TEXT_RE = re.compile(
    r"<function=(?P<name>\w+)>(?P<args>\{.*?\})(?:</function>)?", re.DOTALL
)


def _salvage(content: str) -> list[dict]:
    out = []
    for i, m in enumerate(_FUNC_TEXT_RE.finditer(content or "")):
        name, raw = m.group("name"), m.group("args")
        try:
            json.loads(raw)
        except Exception:
            continue
        out.append({
            "id": f"salvaged_{i}",
            "type": "function",
            "function": {"name": name, "arguments": raw},
        })
    return out


def _llm_call(messages: list, tool_defs: list, model: str, max_retries: int = 3) -> tuple[int, dict | str]:
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                LLM_URL,
                headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tool_defs,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                },
                timeout=HTTP_T,
            )

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 12))
                wait = min(retry_after * (2 ** attempt), 35)
                log.warning("429 rate-limit — waiting %ds (attempt %d/%d)", wait, attempt + 1, max_retries + 1)
                time.sleep(wait)
                continue

            if r.status_code == 400 and "model_decommissioned" in r.text.lower():
                log.error("Decommissioned model: %s", model)
                return 400, "model_decommissioned"

            return (200, r.json()) if r.status_code == 200 else (r.status_code, r.text[:800])

        except requests.RequestException as e:
            log.error("E_LLM_NET %s", e)
            return 0, str(e)

    return 429, "Rate limit persisted after retries."


_SYSTEM_CORE = (
    "You are OmniAgent v3 — a rare intelligence with persistent long-term memory, "
    "self-reflection, and self-evolution.\n\n"
    "CAPABILITIES:\n"
    "1. ASSISTANT — any domain. Use web_search / fetch_url when needed.\n"
    "2. MEMORY AGENT — call remember() on every important fact. "
    "Call recall_tool() proactively when relevant.\n"
    "3. SELF-CRITIC — call reflect() when useful.\n"
    "4. SELF-DEVELOPER — read_self → write_self (COMPLETE file only, no diffs).\n\n"
    "GROUNDING RULE: Facts not in GROUNDED MEMORY and not from a tool are UNKNOWN. "
    "Never fabricate. Offer to search instead.\n\n"
    "After successful self-modification: say 'Done — /reload to activate.'\n"
    "Never remove OWNER_ID guard, boot-test, or brain.load().\n"
    "Tool calls: native tool_calls JSON only."
)


def llm_agent(user_msg: str, user_id: str = "") -> str:
    m = brain.meta()
    count = m.get("interact_count", 0) + 1
    m["interact_count"] = count

    recent = m.setdefault("recent_interactions", [])
    recent.append(user_msg)
    if len(recent) > 20:
        m["recent_interactions"] = recent[-20:]

    m["recall_ids_this_turn"] = []

    if count % DECAY_EVERY == 0 and count % CONSOLIDATE_EVERY != 0:
        log.info("light decay: %s", brain.decay_pass(full=False))

    if count % CONSOLIDATE_EVERY == 0:
        log.info("auto-consolidation at %d", count)
        brain.consolidate(do_commit=True)

    system = _SYSTEM_CORE + brain.memory_context(user_msg, ctx_filter=user_id)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": td["description"],
                "parameters": td["schema"],
            },
        }
        for n, td in TOOLS.items()
    ]

    current_model = MODEL
    fallback_used = False
    rate_limit_count = 0

    for loop_i in range(MAX_LOOPS):
        status, data = _llm_call(messages, tool_defs, current_model)

        if status == 429:
            rate_limit_count += 1
            if rate_limit_count >= 2 and not fallback_used:
                log.warning("Repeated rate limits — switching to fallback model")
                current_model = FALLBACK_MODEL
                fallback_used = True
                messages.append({"role": "user", "content": "Rate limit hit. Using fallback model."})
                continue
            return "⚠️ Groq rate limit reached. Please try again in 15–30 seconds."

        if status == 400 and "model_decommissioned" in str(data).lower():
            log.error("Decommissioned model %s", current_model)
            if not fallback_used:
                current_model = FALLBACK_MODEL
                fallback_used = True
                continue
            return "⚠️ Model unavailable. Please update MODEL or FALLBACK_MODEL in config."

        if status == 0:
            return f"⚠️ network error: {data}"

        if status == 400 and not fallback_used and "tool_use_failed" in str(data):
            log.warning("tool_use_failed → fallback")
            current_model = FALLBACK_MODEL
            fallback_used = True
            messages.append({"role": "user", "content": "Format error. Use native tool_calls JSON only."})
            continue

        if status != 200:
            return f"⚠️ LLM HTTP {status}: {str(data)[:300]}"

        try:
            msg = data["choices"][0]["message"]
        except Exception:
            return "⚠️ malformed LLM response"

        content = msg.get("content") or ""
        calls = msg.get("tool_calls") or []

        if not calls and "<function=" in content:
            salvaged = _salvage(content)
            if salvaged:
                calls = salvaged
                content = ""

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": calls or None,
        })

        if not calls:
            return content or "(empty)"

        for c in calls:
            fname = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if fname not in TOOLS:
                result = f"ERROR: unknown tool '{fname}'"
            else:
                try:
                    result = TOOLS[fname]["fn"](**args)
                except Exception as e:
                    log.exception("E_TOOL %s", fname)
                    result = f"ERROR[E_TOOL]: {type(e).__name__}: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "name": fname,
                "content": str(result)[:MAX_OUT],
            })

    return "⚠️ tool-loop limit reached"