import os, sys, json, time, shutil, logging, subprocess, inspect, re, hashlib
from pathlib import Path
from typing import Callable, Any, get_type_hints

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("omniagent")

# ── env ───────────────────────────────────────────────────────────────────────

def _env(k: str, required: bool = True, default: str = "") -> str:
    v = os.environ.get(k, default)
    if required and not v:
        raise SystemExit(f"FATAL env {k} missing")
    return v

TG_TOKEN       = _env("TG_TOKEN")
OWNER_ID       = int(_env("OWNER_ID"))
LLM_KEY        = _env("LLM_KEY")
LLM_URL        = _env("LLM_URL",        False, "https://api.groq.com/openai/v1/chat/completions")
MODEL          = _env("MODEL",          False, "llama-3.3-70b-versatile")
FALLBACK_MODEL = _env("FALLBACK_MODEL", False, "llama3-groq-70b-8192-tool-use-preview")
GITHUB_REPO    = _env("GITHUB_REPO",   False)
GITHUB_TOKEN   = _env("GITHUB_TOKEN",  False)

SELF       = Path(__file__).resolve()
BACKUP_DIR = SELF.parent / ".backups"
EDGES_LOG  = SELF.parent / "EDGES_LOG.md"
BRAIN_FILE = SELF.parent / "brain.json"
BACKUP_DIR.mkdir(exist_ok=True)

MAX_LOOPS, MAX_OUT, MAX_CHUNK  = 12, 8000, 3500
HTTP_T, SHELL_T, WEB_T         = 120, 30, 15
MAX_SRC                        = 512_000
CONSOLIDATE_EVERY              = 50
DECAY_EVERY                    = 20
FLUSH_INTERVAL                 = 3.0
CONF_DECAY                     = 0.06
CONF_FLOOR                     = 0.15
CONF_BOOST                     = 0.10
CONF_DEFAULT                   = 0.75

# ── tool registry ─────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {}

def _py_to_json(t: Any) -> dict:
    m = {str:"string", int:"integer", float:"number", bool:"boolean", list:"array", dict:"object"}
    return {"type": m.get(t, "string")}

def tool(desc: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        hints = get_type_hints(fn)
        sig   = inspect.signature(fn)
        props, required = {}, []
        for name, p in sig.parameters.items():
            props[name] = _py_to_json(hints.get(name, str))
            if p.default is inspect.Parameter.empty:
                required.append(name)
        TOOLS[fn.__name__] = {
            "fn": fn, "description": desc,
            "schema": {"type":"object","properties":props,"required":required},
        }
        return fn
    return deco

# ═══════════════════════════════════════════════════════════════════════════════
# BRAIN — persistent memory engine (v3)
# ═══════════════════════════════════════════════════════════════════════════════

_brain: dict = {}
_dirty: bool = False
_last_flush: float = 0.0

def _brain_default() -> dict:
    return {
        "version": 3,
        "nodes": {},
        "reflection_log": [],
        "meta": {
            "interact_count": 0, "decay_cycle": 0,
            "consolidation_count": 0, "created": time.time(),
            "last_commit": 0.0, "recent_interactions": []
        }
    }

def brain_load() -> None:
    global _brain, _dirty
    if BRAIN_FILE.exists():
        try:
            data = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
            if data.get("version") == 2:
                data["version"] = 3
                data.setdefault("reflection_log", [])
                data["meta"].setdefault("consolidation_count", 0)
                data["meta"].setdefault("last_commit", 0.0)
                data["meta"].setdefault("recent_interactions", [])
                for node in data.get("nodes", {}).values():
                    node.setdefault("ctx", "")
                    node.setdefault("source", "user")
            if data.get("version") == 3:
                _brain = data
                log.info("brain loaded: %d nodes", len(_brain.get("nodes", {})))
                return
        except Exception as e:
            log.warning("brain load failed (%s) — starting fresh", e)
    _brain = _brain_default()
    _dirty = True
    brain_flush(force=True)

def brain_flush(force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty:
        return
    now = time.time()
    if not force and (now - _last_flush < FLUSH_INTERVAL):
        return
    try:
        BRAIN_FILE.write_text(json.dumps(_brain, indent=2, ensure_ascii=False), encoding="utf-8")
        _dirty = False
        _last_flush = now
    except Exception as e:
        log.error("brain_flush failed: %s", e)

def brain_backup() -> str:
    if not BRAIN_FILE.exists():
        return "no brain file to back up"
    dest = BACKUP_DIR / f"brain_{int(time.time())}.json"
    shutil.copy2(BRAIN_FILE, dest)
    old = sorted(BACKUP_DIR.glob("brain_*.json"))[:-10]
    for f in old:
        f.unlink(missing_ok=True)
    return f"brain backed up → {dest.name}"

def brain_commit() -> str:
    brain_flush(force=True)
    try:
        result = _shell_raw('git add brain.json && git commit -m "brain: auto-checkpoint [skip ci]" && git push')
        if "ERROR" in result or result.startswith("fatal"):
            log.warning("git commit/push issue: %s", result[:200])
            return f"git failed: {result[:100]}"
        _meta()["last_commit"] = time.time()
        return result
    except Exception as e:
        log.error("brain_commit exception: %s", e)
        return f"git error: {e}"

def _key_hash(key: str) -> str:
    return hashlib.md5(key.lower().strip().encode()).hexdigest()[:10]

def _nodes() -> dict:
    return _brain.setdefault("nodes", {})

def _meta() -> dict:
    return _brain.setdefault("meta", {
        "interact_count": 0, "decay_cycle": 0,
        "consolidation_count": 0, "created": time.time(),
        "last_commit": 0.0, "recent_interactions": []
    })

def _reflection_log() -> list:
    return _brain.setdefault("reflection_log", [])

# ── cognitive operations ──────────────────────────────────────────────────────

def brain_learn(key: str, value: str, conf: float = CONF_DEFAULT,
                ctx: str = "", source: str = "user") -> str:
    global _dirty
    key   = key.strip()[:150]
    value = value.strip()[:500]
    conf  = max(0.0, min(1.0, conf))
    khash = _key_hash(key)
    nodes = _nodes()

    if khash in nodes:
        node = nodes[khash]
        if node["value"].lower() == value.lower():
            old = node["conf"]
            node["conf"]   = round(min(1.0, node["conf"] + 0.08), 3)
            node["ts"]     = time.time()
            node["source"] = source
            _dirty = True
            return f"reinforced '{key}': conf {old:.2f}→{node['conf']:.2f}"
        else:
            winner = _adjudicate(key, node["value"], value, node["conf"], conf)
            action = "relearned" if winner["value"] == value else "retained"
            node["value"]  = winner["value"]
            node["conf"]   = round(winner["conf"], 3)
            node["ts"]     = time.time()
            node["source"] = source
            _dirty = True
            return f"{action} '{key}' → '{winner['value'][:60]}' (conf={node['conf']:.2f})"
    else:
        nodes[khash] = {
            "key": key, "value": value, "conf": round(conf, 3),
            "ts": time.time(), "hits": 0,
            "cycle": _meta().get("decay_cycle", 0),
            "ctx": ctx, "source": source
        }
        _dirty = True
        return f"learned '{key}' = '{value[:60]}' (conf={conf:.2f})"

def brain_recall(query: str, top_n: int = 7, ctx_filter: str = "") -> list[dict]:
    global _dirty
    nodes  = _nodes()
    q_tok  = set(re.findall(r'\w+', query.lower()))
    scored = []

    for khash, node in nodes.items():
        if ctx_filter and node.get("ctx") and node["ctx"] != ctx_filter:
            continue
        k_tok   = set(re.findall(r'\w+', node["key"].lower()))
        v_tok   = set(re.findall(r'\w+', node["value"].lower()))
        overlap = len(q_tok & (k_tok | v_tok))
        if node["key"].lower() in query.lower():
            overlap += 6
        score = overlap * node["conf"]
        if score > 0:
            scored.append((score, khash, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    hits = scored[:top_n]
    if not hits:
        return []

    now_cycle = _meta().get("decay_cycle", 0)
    boosted = False
    recall_set = set(_meta().get("recall_ids_this_turn", []))
    for _, khash, node in hits:
        if khash not in recall_set:
            node["conf"]  = round(min(1.0, node["conf"] + CONF_BOOST), 3)
            node["hits"]  = node.get("hits", 0) + 1
            node["cycle"] = now_cycle
            recall_set.add(khash)
            boosted = True
    if boosted:
        _meta()["recall_ids_this_turn"] = list(recall_set)
        _dirty = True

    return [node for _, _, node in hits]

def brain_forget(key: str) -> str:
    global _dirty
    khash = _key_hash(key.strip())
    nodes = _nodes()
    if khash in nodes:
        del nodes[khash]
        _dirty = True
        return f"forgotten '{key}'"
    return f"not found '{key}'"

def brain_decay_pass(full: bool = False) -> str:
    global _dirty
    meta    = _meta()
    nodes   = _nodes()
    cycle   = meta.get("decay_cycle", 0)
    meta["decay_cycle"] = cycle + 1

    decay_amount = CONF_DECAY if full else CONF_DECAY * 0.4
    decayed = pruned = 0
    to_prune = []

    for khash, node in nodes.items():
        if node.get("cycle", 0) < cycle:
            node["conf"] = round(node["conf"] - decay_amount, 3)
            decayed += 1
        if full:
            node["hits"] = 0
        if node["conf"] < CONF_FLOOR:
            to_prune.append(khash)

    if full:
        for khash in to_prune:
            log.info("prune '%s'", nodes[khash]["key"])
            del nodes[khash]
        pruned = len(to_prune)

    _dirty = True
    label = "full" if full else "light"
    return f"decay [{label}] cycle={cycle+1}: {decayed} decayed, {pruned} pruned, {len(nodes)} surviving"

def brain_consolidate(commit: bool = True) -> str:
    meta = _meta()
    since = time.time() - meta.get("last_commit", 0.0)
    do_commit = commit and since > 300

    backup_msg = brain_backup()
    nodes = _nodes()
    if len(nodes) < 5:
        return f"brain too sparse (< 5 nodes). {backup_msg}"

    snapshot = "\n".join(
        f"{n['key']}: {n['value']} (conf={n['conf']:.2f})"
        for n in nodes.values()
    )
    prompt = (
        "You are a memory consolidation engine. Knowledge snapshot:\n"
        f"{snapshot}\n\n"
        "Find up to 3 pairs that express essentially the same fact and should merge. "
        "Reply ONLY as JSON array: "
        '[{"keep":"exact_key","drop":"exact_key","merged_value":"unified fact"},...] '
        "or [] if nothing to merge. No preamble."
    )
    try:
        r = requests.post(LLM_URL,
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json"},
            json={"model":MODEL,"messages":[{"role":"user","content":prompt}],
                  "temperature":0.1,"max_tokens":400},
            timeout=30)
        if r.status_code != 200:
            return f"consolidation LLM failed HTTP {r.status_code}"
        text = r.json()["choices"][0]["message"]["content"].strip()
        m    = re.search(r'\[.*\]', text, re.DOTALL)
        merges = json.loads(m.group()) if m else []
        count  = 0
        for merge in merges:
            keep, drop, val = merge.get("keep",""), merge.get("drop",""), merge.get("merged_value","")
            if keep and drop and val:
                brain_learn(keep, val, conf=0.90, source="agent")
                brain_forget(drop)
                count += 1
    except Exception as e:
        log.warning("E_CONSOLIDATE %s", e)
        count = 0

    decay_result  = brain_decay_pass(full=True)
    meta["consolidation_count"] = meta.get("consolidation_count", 0) + 1
    commit_result = brain_commit() if do_commit else "git commit skipped (rate-limit)"

    return (f"consolidation #{meta['consolidation_count']}: {count} merges | "
            f"{decay_result} | {backup_msg} | git: {commit_result[:60]}")

def _adjudicate(key: str, old_val: str, new_val: str, old_conf: float, new_conf: float) -> dict:
    prompt = (
        f"Conflicting beliefs about '{key}':\n"
        f"A (conf={old_conf:.2f}): {old_val}\n"
        f"B (conf={new_conf:.2f}): {new_val}\n"
        "Reply ONLY: WINNER=A or WINNER=B"
    )
    try:
        r = requests.post(LLM_URL,
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json"},
            json={"model":MODEL,"messages":[{"role":"user","content":prompt}],
                  "temperature":0.0,"max_tokens":10},
            timeout=12)
        if r.status_code == 200:
            reply = r.json()["choices"][0]["message"]["content"].strip()
            if "WINNER=B" in reply:
                return {"value": new_val, "conf": min(1.0, new_conf + 0.10)}
            return {"value": old_val, "conf": min(1.0, old_conf + 0.10)}
    except Exception as e:
        log.warning("E_ADJUDICATE %s", e)
    return {"value": new_val, "conf": new_conf} if new_conf >= old_conf else {"value": old_val, "conf": old_conf}

# ── self-reflection ───────────────────────────────────────────────────────────

def brain_reflect() -> str:
    meta = _meta()
    recent = meta.get("recent_interactions", [])
    if len(recent) < 3:
        return "not enough interactions to reflect on"

    transcript = "\n".join(f"- {msg}" for msg in recent[-10:])
    prompt = (
        "You are OmniAgent's self-critic. Review these recent interactions:\n"
        f"{transcript}\n\n"
        "Identify: (1) missed facts to remember, (2) uncertain answers, "
        "(3) user patterns, (4) one concrete improvement.\n"
        "Reply ONLY as JSON: "
        '{"summary":"2-sentence critique","memories_to_add":[{"key":"k","value":"v"}],'
        '"actions":["action1","action2"]}'
    )
    try:
        r = requests.post(LLM_URL,
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json"},
            json={"model":MODEL,"messages":[{"role":"user","content":prompt}],
                  "temperature":0.3,"max_tokens":500},
            timeout=30)
        if r.status_code != 200:
            return f"reflection LLM failed HTTP {r.status_code}"
        text = r.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return "reflection: no structured output"
        data = json.loads(m.group())

        added = 0
        for mem in data.get("memories_to_add", []):
            if mem.get("key") and mem.get("value"):
                brain_learn(mem["key"], mem["value"], conf=0.65, source="agent")
                added += 1

        log_entry = {
            "ts": time.time(),
            "summary": data.get("summary", ""),
            "actions": data.get("actions", [])
        }
        rlog = _reflection_log()
        rlog.append(log_entry)
        if len(rlog) > 10:
            _brain["reflection_log"] = rlog[-10:]

        global _dirty
        _dirty = True
        return (f"reflection complete: {data.get('summary','')}\n"
                f"memories added: {added}\n"
                f"actions: {'; '.join(data.get('actions', []))}")
    except Exception as e:
        log.warning("E_REFLECT %s", e)
        return f"reflection error: {e}"

def brain_status() -> str:
    nodes = _nodes()
    meta  = _meta()
    if not nodes:
        return "🧠 brain: empty"
    avg_c = sum(n["conf"] for n in nodes.values()) / len(nodes)
    top3  = sorted(nodes.values(), key=lambda n: n["conf"], reverse=True)[:3]
    top_s = " | ".join(f"'{n['key']}'({n['conf']:.2f})" for n in top3)
    rlog  = _reflection_log()
    return (f"🧠 {len(nodes)} nodes | avg_conf={avg_c:.2f} | "
            f"cycle={meta.get('decay_cycle',0)} | "
            f"consolidations={meta.get('consolidation_count',0)} | "
            f"reflections={len(rlog)} | top: {top_s}")

def _build_memory_context(user_msg: str, ctx_filter: str = "") -> str:
    hits = brain_recall(user_msg, top_n=6, ctx_filter=ctx_filter)
    if not hits:
        return ""
    lines = [f"  • {n['key']}: {n['value']}  [conf={n['conf']:.2f}, src={n.get('source','?')}]"
             for n in hits]
    return ("\n\n━━ GROUNDED MEMORY ━━\n"
            "Verified facts from long-term memory. Reason over these. "
            "Never contradict without calling forget() first.\n"
            + "\n".join(lines) + "\n━━━━━━━━━━━━━━━━━━━━")

# ── self-modification ─────────────────────────────────────────────────────────

@tool("Read the full current source of bot.py.")
def read_self() -> str:
    return SELF.read_text(encoding="utf-8")

@tool("Overwrite bot.py with COMPLETE new source. No diffs, no ellipses. Full file only. Syntax + boot-test + symbol-check gated. Auto-backed-up.")
def write_self(new_code: str) -> str:
    if not isinstance(new_code, str) or not new_code.strip():
        return "REJECTED: empty"
    for marker in ["# ... existing","# ... rest of","# ...existing","# ...rest of",
                   "# existing code","...existing...","# <rest of file>"]:
        if marker in new_code:
            return f"REJECTED: diff marker '{marker}' — provide COMPLETE file. Call read_self first."
    if len(new_code.encode()) > MAX_SRC:
        return f"REJECTED: exceeds {MAX_SRC} bytes"
    try:
        compile(new_code, str(SELF), "exec")
    except SyntaxError as e:
        return f"REJECTED: SyntaxError line {e.lineno}: {e.msg}"
    tmp = SELF.with_suffix(".candidate.py")
    tmp.write_text(new_code, encoding="utf-8")
    env = os.environ.copy()
    env["OMNI_BOOT_TEST"] = "1"
    try:
        r = subprocess.run([sys.executable, str(tmp)],
                           capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return "REJECTED: boot-test timeout"
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: boot-test failed\n{(r.stderr or r.stdout)[-1200:]}"

    required_symbols = ["OWNER_ID", "write_self", "brain_load", "llm_agent",
                        "OMNI_BOOT_TEST", "BRAIN_FILE", "brain_flush"]
    missing = [s for s in required_symbols if s not in new_code]
    if missing:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: missing critical symbols: {missing}"

    backup = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, backup)
    tmp.replace(SELF)
    log.info("self-patch ok backup=%s", backup.name)
    return f"ok — backup={backup.name}. Tell user to /reload."

# ── capability tools ──────────────────────────────────────────────────────────

def _shell_raw(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=SHELL_T)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_OUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout {SHELL_T}s"
    except Exception as e:
        return f"ERROR: {e}"

@tool("Run a shell command (30s timeout).")
def shell(cmd: str) -> str:
    if not cmd.strip(): return "ERROR: empty cmd"
    return _shell_raw(cmd)

@tool("Commit all changes and push to origin.")
def git_push(msg: str) -> str:
    if not msg.strip(): return "ERROR: empty msg"
    safe = msg.replace('"',"'")[:200]
    return _shell_raw(f'git add -A && git commit -m "{safe}" && git push')

@tool("Append an edge case to EDGES_LOG.md.")
def log_edge(description: str) -> str:
    if not description.strip(): return "ERROR: empty"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"

@tool("Search the web via DuckDuckGo.")
def web_search(query: str) -> str:
    if not query.strip(): return "ERROR: empty"
    try:
        r = requests.get("https://api.duckduckgo.com/",
            params={"q":query,"format":"json","no_html":1,"skip_disambig":1},
            timeout=WEB_T, headers={"User-Agent":"OmniAgent/3.0"})
        if r.status_code != 200: return f"ERROR: DDG HTTP {r.status_code}"
        d = r.json()
        chunks = []
        if d.get("AbstractText"): chunks.append(f"Summary: {d['AbstractText']}")
        if d.get("Answer"):       chunks.append(f"Answer: {d['Answer']}")
        for t in (d.get("RelatedTopics") or [])[:6]:
            if isinstance(t, dict) and t.get("Text"):
                chunks.append(f"- {t['Text']}")
        return "\n".join(chunks)[:MAX_OUT] or "(no instant answer)"
    except Exception as e:
        return f"ERROR[E_WEB]: {e}"

@tool("Fetch a URL and return stripped plain text.")
def fetch_url(url: str) -> str:
    if not url.startswith(("http://","https://")): return "ERROR: invalid scheme"
    try:
        r = requests.get(url, timeout=WEB_T, headers={"User-Agent":"OmniAgent/3.0"})
        t = re.sub(r"<script[^>]*>.*?</script>","",r.text,flags=re.DOTALL|re.I)
        t = re.sub(r"<style[^>]*>.*?</style>","",t,flags=re.DOTALL|re.I)
        t = re.sub(r"<[^>]+>"," ",t)
        t = re.sub(r"\s+"," ",t).strip()
        return t[:MAX_OUT]
    except Exception as e:
        return f"ERROR[E_FETCH]: {e}"

@tool("Store an important fact in long-term brain memory.")
def remember(key: str, value: str, confidence: float = 0.75, source: str = "user") -> str:
    return brain_learn(key, value, confidence, source=source)

@tool("Retrieve memories relevant to a topic.")
def recall(topic: str) -> str:
    hits = brain_recall(topic, top_n=8)
    if not hits: return f"no memories found for '{topic}'"
    return "\n".join(f"• {n['key']}: {n['value']} (conf={n['conf']:.2f}, src={n.get('source','?')})" for n in hits)

@tool("Explicitly erase a memory by its key.")
def forget(key: str) -> str:
    return brain_forget(key)

@tool("Show brain memory statistics.")
def brain_info() -> str:
    return brain_status()

@tool("Trigger self-reflection on recent interactions.")
def reflect() -> str:
    return brain_reflect()

# ── LLM engine ────────────────────────────────────────────────────────────────

_FUNC_TEXT_RE = re.compile(r"<function=(?P<name>\w+)>(?P<args>\{.*?\})(?:</function>)?", re.DOTALL)

def _salvage_text_tool_calls(content: str) -> list[dict]:
    out = []
    for i, m in enumerate(_FUNC_TEXT_RE.finditer(content or "")):
        name, raw = m.group("name"), m.group("args")
        try:
            json.loads(raw)
        except:
            continue
        out.append({"id":f"salvaged_{i}","type":"function","function":{"name":name,"arguments":raw}})
    return out

def _llm_call(messages: list, tool_defs: list, model: str) -> tuple[int, dict | str]:
    try:
        r = requests.post(LLM_URL,
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json"},
            json={"model":model,"messages":messages,"tools":tool_defs,
                  "tool_choice":"auto","temperature":0.2},
            timeout=HTTP_T)
        return (200, r.json()) if r.status_code == 200 else (r.status_code, r.text[:800])
    except requests.RequestException as e:
        log.error("E_LLM_NET %s", e)
        return 0, str(e)

SYSTEM_CORE = """You are OmniAgent v3 — a rare intelligence with persistent long-term memory, self-reflection, and self-evolution.

CAPABILITIES:
1. ASSISTANT — any domain. Use web_search / fetch_url when needed.
2. MEMORY AGENT — call remember() on important facts. Call recall() proactively.
3. SELF-CRITIC — call reflect() when useful.
4. SELF-DEVELOPER — read_self → write_self (COMPLETE file only).

GROUNDING RULE: Facts not in GROUNDED MEMORY and not from tools are UNKNOWN. Never fabricate.

After successful self-modification: 'Done — /reload to activate.'
Never remove OWNER_ID guard, boot-test, or brain_load().
Tool calls: native tool_calls JSON only."""

def llm_agent(user_msg: str, user_id: str = "") -> str:
    global _dirty
    meta = _meta()
    meta["interact_count"] = meta.get("interact_count", 0) + 1

    recent = meta.setdefault("recent_interactions", [])
    recent.append(user_msg)
    if len(recent) > 20:
        meta["recent_interactions"] = recent[-20:]

    _meta().setdefault("recall_ids_this_turn", [])

    if meta["interact_count"] % DECAY_EVERY == 0 and meta["interact_count"] % CONSOLIDATE_EVERY != 0:
        result = brain_decay_pass(full=False)
        log.info("light decay: %s", result)

    if meta["interact_count"] % CONSOLIDATE_EVERY == 0:
        log.info("auto-consolidation at %d", meta["interact_count"])
        result = brain_consolidate(commit=True)
        log.info("consolidation: %s", result)

    mem_ctx   = _build_memory_context(user_msg, ctx_filter=user_id)
    system    = SYSTEM_CORE + mem_ctx
    messages  = [{"role":"system","content":system}, {"role":"user","content":user_msg}]
    tool_defs = [
        {"type":"function","function":{"name":n,"description":m["description"],"parameters":m["schema"]}}
        for n, m in TOOLS.items()
    ]

    current_model = MODEL
    fallback_used = False

    for loop_i in range(MAX_LOOPS):
        status, data = _llm_call(messages, tool_defs, current_model)
        if status == 0:
            return f"⚠️ network error: {data}"
        if status == 400 and not fallback_used and "tool_use_failed" in str(data):
            current_model = FALLBACK_MODEL
            fallback_used = True
            messages.append({"role":"user","content": "Format error. Use native tool_calls JSON only."})
            continue
        if status != 200:
            return f"⚠️ LLM HTTP {status}: {str(data)[:300]}"

        try:
            msg = data["choices"][0]["message"]
        except Exception:
            return "⚠️ malformed LLM response"

        content = msg.get("content") or ""
        calls   = msg.get("tool_calls") or []

        if not calls and "<function=" in content:
            salvaged = _salvage_text_tool_calls(content)
            if salvaged:
                calls = salvaged
                content = ""

        messages.append({"role":"assistant","content":content,"tool_calls":calls or None})
        if not calls:
            return content or "(empty)"

        for c in calls:
            fname = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except:
                args = {}
            if fname not in TOOLS:
                result = f"ERROR: unknown tool '{fname}'"
            else:
                try:
                    result = TOOLS[fname]["fn"](**args)
                except Exception as e:
                    log.exception("E_TOOL %s", fname)
                    result = f"ERROR[E_TOOL]: {type(e).__name__}: {e}"
            messages.append({"role":"tool","tool_call_id":c["id"],"name":fname,
                             "content":str(result)[:MAX_OUT]})
    return "⚠️ tool-loop limit reached"

# ── multi-user ────────────────────────────────────────────────────────────────

ALLOWED_USERS: set[int] = set()
_allowed_raw = os.environ.get("ALLOWED_USERS", "")
if _allowed_raw:
    for uid in _allowed_raw.split(","):
        try:
            ALLOWED_USERS.add(int(uid.strip()))
        except ValueError:
            pass

def is_authorized(u: Update) -> bool:
    uid = u.effective_user.id if u.effective_user else None
    return uid == OWNER_ID or uid in ALLOWED_USERS

def is_owner(u: Update) -> bool:
    return bool(u.effective_user and u.effective_user.id == OWNER_ID)

def user_ctx(u: Update) -> str:
    uid = u.effective_user.id if u.effective_user else OWNER_ID
    return "" if uid == OWNER_ID else f"user_{uid}"

# ── github & helpers ──────────────────────────────────────────────────────────

def audit_repo_privacy() -> str:
    if not GITHUB_REPO: return "unconfigured"
    try:
        h = {"Accept":"application/vnd.github+json"}
        if GITHUB_TOKEN: h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}", headers=h, timeout=10)
        if r.status_code == 200:
            return "private" if r.json().get("private") else "public"
        return "unknown"
    except:
        return "unknown"

# ── telegram handlers ─────────────────────────────────────────────────────────

async def send_long(update: Update, text: str) -> None:
    text = text or "(empty)"
    for i in range(0, len(text), MAX_CHUNK):
        await update.message.reply_text(text[i:i + MAX_CHUNK])

async def handle_query(update: Update, text: str) -> None:
    if not text:
        await update.message.reply_text("Say something.")
        return
    await update.message.reply_text("🧠 …")
    ctx = user_ctx(update)
    try:
        reply = llm_agent(text, user_id=ctx)
    except Exception as e:
        log.exception("E_AGENT")
        reply = f"💥 {e}"
    await send_long(update, reply)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    role = "owner" if is_owner(u) else "guest"
    await u.message.reply_text(
        f"🤖 OmniAgent v3 [{role}]\n{brain_status()}\n\n"
        "I remember. I learn. I reflect. I evolve.\n\n"
        "Commands: /brain /remember /forget /reflect /consolidate /health"
    )

async def cmd_ask(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    q = " ".join(c.args).strip()
    if not q and u.message.reply_to_message:
        q = u.message.reply_to_message.text or ""
    await handle_query(u, q)

async def cmd_think(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_ask(u, c)

async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    t = (u.message.text or "").strip()
    if t and not t.startswith("/"):
        await handle_query(u, t)

async def cmd_reload(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("♻️ restarting…")
    os.execv(sys.executable, [sys.executable, str(SELF)])

async def cmd_src(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    with SELF.open("rb") as f:
        await u.message.reply_document(document=f, filename="bot.py")

async def cmd_rollback(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    backups = sorted(BACKUP_DIR.glob("bot_*.py"))
    if not backups:
        await u.message.reply_text("No backups.")
        return
    shutil.copy2(backups[-1], SELF)
    await u.message.reply_text(f"✅ restored {backups[-1].name}. /reload.")

async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    priv = audit_repo_privacy()
    warn = "\n⚠️ REPO IS PUBLIC — rotate secrets" if priv == "public" else ""
    meta = _meta()
    next_c = CONSOLIDATE_EVERY - (meta.get("interact_count", 0) % CONSOLIDATE_EVERY or CONSOLIDATE_EVERY)
    await u.message.reply_text(
        f"✅ OmniAgent v3\nmodel: {MODEL} | fallback: {FALLBACK_MODEL}\n"
        f"interactions: {meta.get('interact_count',0)} | next consolidation: {next_c}\n"
        f"privacy: {priv}{warn}\n{brain_status()}"
    )

async def cmd_tools(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    lines = [f"• {n} — {m['description']}" for n, m in TOOLS.items()]
    await u.message.reply_text("🛠 Tools:\n" + "\n".join(lines))

async def cmd_model(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text(f"primary: {MODEL}\nfallback: {FALLBACK_MODEL}")

async def cmd_brain(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    args = " ".join(c.args).strip().lower()
    if args == "save":
        brain_flush(force=True)
        result = brain_commit()
        await u.message.reply_text(f"🧠 saved + committed\n{result[:200]}")
        return
    if args == "backup":
        await u.message.reply_text(f"🧠 {brain_backup()}")
        return

    nodes = _nodes()
    if not nodes:
        await u.message.reply_text("🧠 Brain is empty.")
        return
    lines = []
    for n in sorted(nodes.values(), key=lambda x: x["conf"], reverse=True):
        age_h = (time.time() - n["ts"]) / 3600
        ctx_s = f" [{n['ctx']}]" if n.get("ctx") else ""
        lines.append(f"[{n['conf']:.2f}] {n['key']}: {n['value'][:80]} (age:{age_h:.0f}h src:{n.get('source','?')}{ctx_s})")
    await send_long(u, f"🧠 Brain — {len(nodes)} nodes\n\n" + "\n".join(lines))

async def cmd_remember(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    args = " ".join(c.args).strip()
    if "::" not in args:
        await u.message.reply_text("Usage: /remember key :: value")
        return
    key, _, val = args.partition("::")
    ctx = user_ctx(u)
    await u.message.reply_text(f"🧠 {brain_learn(key.strip(), val.strip(), ctx=ctx, source='manual')}")

async def cmd_forget(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    key = " ".join(c.args).strip()
    if not key:
        await u.message.reply_text("Usage: /forget key")
        return
    await u.message.reply_text(f"🧠 {brain_forget(key)}")

async def cmd_consolidate(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("🧠 consolidating…")
    result = brain_consolidate(commit=True)
    await send_long(u, f"🧠 {result}")

async def cmd_reflect(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("🧠 reflecting…")
    result = brain_reflect()
    await send_long(u, f"🧠 {result}")

async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("E_TG %s", c.error, exc_info=c.error)

# ── boot ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if os.environ.get("OMNI_BOOT_TEST") == "1":
        brain_load()
        # Deeper smoke test
        try:
            test_reply = llm_agent("Hello, test boot.")
            assert "brain" in test_reply.lower() or len(test_reply) > 10
        except Exception as e:
            print(f"smoke test failed: {e}")
            sys.exit(1)
        assert callable(llm_agent)
        assert callable(brain_learn)
        assert callable(brain_flush)
        print("boot-test ok")
        sys.exit(0)

    priv = audit_repo_privacy()
    log.info("privacy=%s tools=%d model=%s allowed_users=%d", priv, len(TOOLS), MODEL, len(ALLOWED_USERS))
    if priv == "public":
        log.warning("REPO IS PUBLIC — rotate secrets immediately")

    brain_load()

    app = ApplicationBuilder().token(TG_TOKEN).build()
    for name, fn in [
        ("start", cmd_start), ("ask", cmd_ask), ("think", cmd_think),
        ("reload", cmd_reload), ("src", cmd_src), ("rollback", cmd_rollback),
        ("health", cmd_health), ("tools", cmd_tools), ("model", cmd_model),
        ("brain", cmd_brain), ("remember", cmd_remember), ("forget", cmd_forget),
        ("consolidate", cmd_consolidate), ("reflect", cmd_reflect),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("OmniAgent v3 — up. model=%s owner=%s", MODEL, OWNER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()