import os, sys, json, time, shutil, logging, subprocess, inspect, re
from pathlib import Path
from typing import Callable, Any, get_type_hints

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("omniagent")

def _env(k: str, required: bool = True, default: str = "") -> str:
    v = os.environ.get(k, default)
    if required and not v:
        raise SystemExit(f"FATAL env {k} missing")
    return v

TG_TOKEN       = _env("TG_TOKEN")
OWNER_ID       = int(_env("OWNER_ID"))
LLM_KEY        = _env("LLM_KEY")
LLM_URL        = _env("LLM_URL", False, "https://api.groq.com/openai/v1/chat/completions")
MODEL          = _env("MODEL",          False, "llama-3.3-70b-versatile")
FALLBACK_MODEL = _env("FALLBACK_MODEL", False, "llama3-groq-70b-8192-tool-use-preview")
GITHUB_REPO    = _env("GITHUB_REPO",  False)
GITHUB_TOKEN   = _env("GITHUB_TOKEN", False)

SELF       = Path(__file__).resolve()
BACKUP_DIR = SELF.parent / ".backups"
EDGES_LOG  = SELF.parent / "EDGES_LOG.md"
BACKUP_DIR.mkdir(exist_ok=True)

MAX_LOOPS, MAX_OUT, MAX_CHUNK = 10, 8000, 3500
HTTP_T, SHELL_T, WEB_T        = 120, 30, 15
MAX_SRC                       = 512_000

TOOLS: dict[str, dict] = {}

def _py_to_json(t: Any) -> dict:
    m = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    return {"type": m.get(t, "string")}

def tool(desc: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        hints = get_type_hints(fn)
        sig   = inspect.signature(fn)
        props, required = {}, []
        for name, p in sig.parameters.items():
            t = hints.get(name, str)
            props[name] = _py_to_json(t)
            if p.default is inspect.Parameter.empty:
                required.append(name)
        TOOLS[fn.__name__] = {
            "fn": fn,
            "description": desc,
            "schema": {"type": "object", "properties": props, "required": required},
        }
        return fn
    return deco

@tool("Read the full current source of bot.py.")
def read_self() -> str:
    return SELF.read_text(encoding="utf-8")

@tool("Overwrite bot.py with COMPLETE new source. MUST be the entire file — no diffs, no ellipses, no '# ... existing code'. Syntax + subprocess boot-test gated. Auto-backed-up.")
def write_self(new_code: str) -> str:
    if not isinstance(new_code, str) or not new_code.strip():
        return "REJECTED: empty code"
    # Detect diff/stub patterns — no line-count heuristic (too aggressive for small bots)
    DIFF_MARKERS = [
        "# ... existing", "# ... rest of", "# ...existing", "# ...rest of",
        "# existing code", "...existing...", "# <rest of file>",
    ]
    for marker in DIFF_MARKERS:
        if marker in new_code:
            return (
                "REJECTED: looks like a diff or stub — detected placeholder "
                f"'{marker}'. Provide the COMPLETE file (no ellipses, no placeholders). "
                "Call read_self first and return the entire modified source."
            )
    if len(new_code.encode("utf-8")) > MAX_SRC:
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
        r = subprocess.run([sys.executable, str(tmp)], capture_output=True, text=True, timeout=8, env=env)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return "REJECTED: boot-test timed out"
    if r.returncode != 0:
        err = (r.stderr or r.stdout)[-1200:]
        tmp.unlink(missing_ok=True)
        return f"REJECTED: boot-test failed\n{err}"
    backup = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, backup)
    tmp.replace(SELF)
    log.info("self-patch ok backup=%s", backup.name)
    return f"ok backup={backup.name} — tell user to /reload"

@tool("Run a shell command with 30s timeout.")
def shell(cmd: str) -> str:
    if not cmd.strip():
        return "ERROR: empty cmd"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=SHELL_T)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_OUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout {SHELL_T}s"
    except Exception as e:
        log.exception("E_SHELL")
        return f"ERROR[E_SHELL]: {e}"

@tool("Commit all changes and push to origin.")
def git_push(msg: str) -> str:
    if not msg.strip():
        return "ERROR: empty msg"
    safe = msg.replace('"', "'")[:200]
    return shell(f'git add -A && git commit -m "{safe}" && git push')

@tool("Append an edge case to EDGES_LOG.md.")
def log_edge(description: str) -> str:
    if not description.strip():
        return "ERROR: empty"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"

@tool("Search the web via DuckDuckGo for current info.")
def web_search(query: str) -> str:
    if not query.strip():
        return "ERROR: empty query"
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=WEB_T, headers={"User-Agent": "OmniAgent/1.0"},
        )
        if r.status_code != 200:
            return f"ERROR: DDG HTTP {r.status_code}"
        d = r.json()
        chunks = []
        if d.get("AbstractText"): chunks.append(f"Summary: {d['AbstractText']}")
        if d.get("Answer"):       chunks.append(f"Answer: {d['Answer']}")
        for t in (d.get("RelatedTopics") or [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                chunks.append(f"- {t['Text']}")
        return "\n".join(chunks)[:MAX_OUT] or f"(no instant answer for '{query}')"
    except Exception as e:
        log.warning("E_WEB %s", e)
        return f"ERROR[E_WEB]: {e}"

@tool("Fetch a URL and return stripped plain text.")
def fetch_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: url must start with http(s)"
    try:
        r = requests.get(url, timeout=WEB_T, headers={"User-Agent": "OmniAgent/1.0"})
        t = r.text
        t = re.sub(r"<script[^>]*>.*?</script>", "", t, flags=re.DOTALL | re.I)
        t = re.sub(r"<style[^>]*>.*?</style>",   "", t, flags=re.DOTALL | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:MAX_OUT]
    except Exception as e:
        log.warning("E_FETCH %s", e)
        return f"ERROR[E_FETCH]: {e}"

def audit_repo_privacy() -> str:
    if not GITHUB_REPO:
        return "unconfigured"
    try:
        h = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}", headers=h, timeout=10)
        if r.status_code == 404:
            return "private" if not GITHUB_TOKEN else "unknown"
        if r.status_code == 200:
            return "private" if r.json().get("private") else "public"
        return "unknown"
    except Exception as e:
        log.warning("E_AUDIT %s", e)
        return "unknown"

SYSTEM_PROMPT = """You are OmniAgent, the owner's private cross-domain assistant on Telegram.

Two roles:
1 GENERAL ASSISTANT — answer any domain. Use web_search for current info. Use fetch_url for links. Be concise.
2 SELF-DEVELOPER — when asked to improve the bot:
   - First call read_self to get current source.
   - Then call write_self with the COMPLETE rewritten file.
   - CRITICAL: write_self requires the ENTIRE file contents. Never use '# ... existing code', '...', or any placeholder. Copy every line from read_self output and modify only what's needed.
   - After success say 'Done — send /reload to activate.'
   - Never remove the OWNER_ID guard or the boot-test in write_self.

Tool-call format: you MUST use the native tool_calls JSON structure. Do NOT emit '<function=name>{...}' as plain text — that format is rejected. Use structured tool_calls only."""

# Regex for salvaging text-wrapped tool calls emitted by misbehaving models.
# Named groups make extraction explicit and safe.
_FUNC_TEXT_RE = re.compile(
    r"<function=(?P<name>\w+)>(?P<args>\{.*?\})(?:</function>)?",
    re.DOTALL,
)

def _salvage_text_tool_calls(content: str) -> list[dict]:
    """
    Convert <function=name>{...}</function> text blobs into proper tool_calls
    dicts. IDs use a simple counter so assistant-message IDs and tool-result
    tool_call_ids always match exactly (no timestamp drift between the two).
    """
    out = []
    for i, m in enumerate(_FUNC_TEXT_RE.finditer(content or "")):
        name = m.group("name")
        raw  = m.group("args")
        try:
            json.loads(raw)   # validate JSON before accepting
        except json.JSONDecodeError:
            log.warning("salvage: invalid JSON for tool %s — skipped", name)
            continue
        call_id = f"salvaged_{i}"   # stable, counter-only; matches tool result below
        out.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": raw},
        })
    return out

def _llm_call(messages: list, tool_defs: list, model: str) -> tuple[int, dict | str]:
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
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, r.text[:800]
    except requests.RequestException as e:
        log.error("E_LLM_NET %s", e)
        return 0, str(e)

def llm_agent(user_msg: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]
    tool_defs = [
        {"type": "function", "function": {"name": n, "description": m["description"], "parameters": m["schema"]}}
        for n, m in TOOLS.items()
    ]
    current_model = MODEL
    fallback_used = False   # Fix: ensure the 400-retry path fires at most once

    for loop_i in range(MAX_LOOPS):
        status, data = _llm_call(messages, tool_defs, current_model)

        if status == 0:
            return f"⚠️ network: {data}"

        # --- HTTP 400 / tool_use_failed recovery (single attempt only) ---
        if status == 400 and not fallback_used and "tool_use_failed" in str(data):
            log.warning(
                "tool_use_failed on %s (loop %d) — switching to fallback %s",
                current_model, loop_i, FALLBACK_MODEL,
            )
            current_model = FALLBACK_MODEL
            fallback_used = True
            messages.append({
                "role": "user",
                "content": (
                    "Your previous tool call failed format validation. "
                    "Rules: (1) use native tool_calls JSON — never <function=...> text; "
                    "(2) for write_self provide the COMPLETE file, no ellipses or placeholders."
                ),
            })
            continue

        if status != 200:
            log.error("E_LLM_HTTP %s %s", status, data)
            return f"⚠️ LLM HTTP {status}: {str(data)[:300]}"

        try:
            msg = data["choices"][0]["message"]
        except Exception as e:
            log.error("E_LLM_FMT %s", e)
            return "⚠️ malformed response"

        content = msg.get("content") or ""
        calls   = msg.get("tool_calls") or []

        # --- Salvage text-wrapped tool calls ---
        if not calls and "<function=" in content:
            salvaged = _salvage_text_tool_calls(content)
            if salvaged:
                log.warning("salvaged %d text-wrapped tool call(s) loop=%d", len(salvaged), loop_i)
                calls   = salvaged
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
            except json.JSONDecodeError:
                args = {}
            if fname not in TOOLS:
                result = f"ERROR: unknown tool {fname}"
            else:
                try:
                    result = TOOLS[fname]["fn"](**args)
                except Exception as e:
                    log.exception("E_TOOL %s", fname)
                    result = f"ERROR[E_TOOL]: {type(e).__name__}: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"],   # matches salvaged_N or real ID
                "name": fname,
                "content": str(result)[:MAX_OUT],
            })

    return "⚠️ tool-loop limit"

def is_owner(u: Update) -> bool:
    return bool(u.effective_user and u.effective_user.id == OWNER_ID)

async def send_long(update: Update, text: str) -> None:
    text = text or "(empty)"
    for i in range(0, len(text), MAX_CHUNK):
        await update.message.reply_text(text[i:i + MAX_CHUNK])

async def handle_query(update: Update, text: str) -> None:
    if not text:
        await update.message.reply_text("Say something.")
        return
    await update.message.reply_text("🧠 …")
    try:
        reply = llm_agent(text)
    except Exception as e:
        log.exception("E_AGENT")
        reply = f"💥 [E_AGENT]: {e}"
    await send_long(update, reply)

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(u):
        await u.message.reply_text(
            "🤖 OmniAgent online.\nText me anything — any domain. Web + URL + self-patch.\n"
            "Commands: /ask /think /reload /src /rollback /health /tools /model"
        )

async def cmd_ask(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    q = " ".join(c.args).strip()
    if not q and u.message.reply_to_message:
        q = u.message.reply_to_message.text or ""
    await handle_query(u, q)

async def cmd_think(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_ask(u, c)

async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
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
    latest = backups[-1]
    shutil.copy2(latest, SELF)
    await u.message.reply_text(f"✅ restored {latest.name}. /reload.")

async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    size  = SELF.stat().st_size
    bkps  = len(list(BACKUP_DIR.glob("bot_*.py")))
    priv  = audit_repo_privacy()
    warn  = "\n⚠️ REPO IS PUBLIC — rotate secrets" if priv == "public" else ""
    await u.message.reply_text(
        f"✅ alive\nprimary model: {MODEL}\nfallback: {FALLBACK_MODEL}\n"
        f"source: {size} bytes\nbackups: {bkps}\ntools: {len(TOOLS)}\nprivacy: {priv}{warn}"
    )

async def cmd_tools(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    lines = [f"• {n} — {m['description']}" for n, m in TOOLS.items()]
    await u.message.reply_text("🛠 Tools:\n" + "\n".join(lines))

async def cmd_model(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text(
        f"primary:  {MODEL}\nfallback: {FALLBACK_MODEL}\n(override via MODEL / FALLBACK_MODEL env vars)"
    )

async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("E_TG %s", c.error, exc_info=c.error)

def main() -> None:
    if os.environ.get("OMNI_BOOT_TEST") == "1":
        print("boot-test ok")
        sys.exit(0)
    priv = audit_repo_privacy()
    log.info("privacy=%s tools=%d model=%s fallback=%s", priv, len(TOOLS), MODEL, FALLBACK_MODEL)
    if priv == "public":
        log.warning("REPO IS PUBLIC — rotate any hardcoded secrets")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    for name, fn in [
        ("start", cmd_start), ("ask", cmd_ask), ("think", cmd_think),
        ("reload", cmd_reload), ("src", cmd_src), ("rollback", cmd_rollback),
        ("health", cmd_health), ("tools", cmd_tools), ("model", cmd_model),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("OmniAgent up model=%s owner=%s", MODEL, OWNER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
