"""
OmniAgent — Self-Evolving Telegram Assistant
============================================
Single-file bot. LLM (Groq/Llama-3.3-70B) has tool access to its own
source file and can patch itself live from Telegram chat. Also answers
any domain question and can search the web for current info.

Env vars required:
  TG_TOKEN   - Telegram bot token
  OWNER_ID   - Your Telegram numeric user id
  LLM_KEY    - Groq API key

Optional:
  GITHUB_REPO   - "owner/repo" for privacy self-audit (e.g. "Lovisuals/omniagent")
  GITHUB_TOKEN  - PAT with repo:read scope (for private repo audit)

Commands:
  /start     - wake
  /ask <q>   - ask anything (plain text also works)
  /think <x> - dev task (same as /ask; kept for muscle memory)
  /reload    - restart after self-patch
  /src       - download current bot.py
  /rollback  - restore previous backup
  /health    - status + privacy audit
"""
import os
import sys
import json
import time
import shutil
import logging
import traceback
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("omniagent")

# ─── Config ───────────────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    v = os.environ.get(name)
    assert v, f"FATAL: env var {name} is missing"
    return v

TG_TOKEN = _require_env("TG_TOKEN")
OWNER_ID = int(_require_env("OWNER_ID"))
LLM_KEY  = _require_env("LLM_KEY")

LLM_URL      = os.environ.get("LLM_URL", "https://api.groq.com/openai/v1/chat/completions")
MODEL        = os.environ.get("MODEL",   "llama-3.3-70b-versatile")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "")   # "owner/repo"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

SELF        = Path(__file__).resolve()
BACKUP_DIR  = SELF.parent / ".backups"
EDGES_LOG   = SELF.parent / "EDGES_LOG.md"
BACKUP_DIR.mkdir(exist_ok=True)

# Resource bounds
MAX_TOOL_LOOPS   = 8
MAX_TOOL_OUTPUT  = 8_000
MAX_TG_CHUNK     = 3_500
HTTP_TIMEOUT     = 120
SHELL_TIMEOUT    = 30
MAX_SOURCE_BYTES = 512_000
WEB_TIMEOUT      = 15

# ─── Authorization ────────────────────────────────────────────────────────
def is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == OWNER_ID)

# ─── Privacy self-audit ───────────────────────────────────────────────────
def audit_repo_privacy() -> str:
    """
    Returns one of: 'private', 'public', 'unknown', 'unconfigured'.
    Uses GitHub API. If repo is public, logs a loud warning.
    """
    if not GITHUB_REPO:
        return "unconfigured"
    try:
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}",
            headers=headers, timeout=10,
        )
        if r.status_code == 404:
            # Private + no token, or nonexistent
            return "private" if not GITHUB_TOKEN else "unknown"
        if r.status_code == 200:
            is_private = r.json().get("private", False)
            if not is_private:
                log.warning("⚠️ REPO IS PUBLIC — rotate any hardcoded secrets now")
            return "private" if is_private else "public"
        return "unknown"
    except Exception as e:
        log.warning("privacy audit failed [E_AUDIT]: %s", e)
        return "unknown"

# ─── Self-modification tools ──────────────────────────────────────────────
def tool_read_self() -> str:
    return SELF.read_text(encoding="utf-8")

def tool_write_self(new_code: str) -> str:
    """
    SIDE EFFECT: overwrites bot.py. Why necessary and unavoidable:
    self-evolution is the core feature.
    """
    assert isinstance(new_code, str) and new_code.strip(), "new_code must be non-empty"
    assert len(new_code.encode("utf-8")) <= MAX_SOURCE_BYTES, \
        f"new_code exceeds {MAX_SOURCE_BYTES} bytes"
    try:
        compile(new_code, str(SELF), "exec")
    except SyntaxError as e:
        return f"REJECTED: SyntaxError at line {e.lineno}: {e.msg}"

    backup = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, backup)
    tmp = SELF.with_suffix(".py.tmp")
    tmp.write_text(new_code, encoding="utf-8")
    tmp.replace(SELF)
    log.info("self-patch applied, backup=%s", backup.name)
    return f"ok — backup saved as {backup.name}. Tell user to send /reload."

def tool_shell(cmd: str) -> str:
    """SIDE EFFECT: executes host command. Owner-guard enforced upstream."""
    assert isinstance(cmd, str) and cmd.strip(), "cmd required"
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=SHELL_TIMEOUT
        )
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_TOOL_OUTPUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: shell exceeded {SHELL_TIMEOUT}s"
    except Exception as e:
        log.exception("shell error [E_SHELL]")
        return f"ERROR[E_SHELL]: {e}"

def tool_git_push(msg: str) -> str:
    assert msg.strip(), "commit message required"
    safe = msg.replace('"', "'")[:200]
    return tool_shell(f'git add -A && git commit -m "{safe}" && git push')

def tool_log_edge(description: str) -> str:
    assert description.strip(), "description required"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"

# ─── World-access tools (for "across any domain") ─────────────────────────
def tool_web_search(query: str) -> str:
    """
    Free web search via DuckDuckGo Instant Answer + HTML scrape fallback.
    No API key required.
    """
    assert query.strip(), "query required"
    try:
        # Try DDG instant answer first (structured)
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=WEB_TIMEOUT,
            headers={"User-Agent": "OmniAgent/1.0"},
        )
        if r.status_code == 200:
            data = r.json()
            chunks = []
            if data.get("AbstractText"):
                chunks.append(f"Summary: {data['AbstractText']}")
            if data.get("Answer"):
                chunks.append(f"Answer: {data['Answer']}")
            for topic in (data.get("RelatedTopics") or [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    chunks.append(f"- {topic['Text']}")
            if chunks:
                return "\n".join(chunks)[:MAX_TOOL_OUTPUT]
        return f"(no instant answer for '{query}'. Consider rephrasing.)"
    except Exception as e:
        log.warning("web search failed [E_WEB]: %s", e)
        return f"ERROR[E_WEB]: {e}"

def tool_fetch_url(url: str) -> str:
    """Fetch a URL's text content (stripped, truncated)."""
    assert url.startswith(("http://", "https://")), "url must start with http(s)"
    try:
        r = requests.get(
            url, timeout=WEB_TIMEOUT,
            headers={"User-Agent": "OmniAgent/1.0"},
        )
        text = r.text
        # Cheap HTML strip — no bs4 dependency
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_TOOL_OUTPUT]
    except Exception as e:
        log.warning("fetch failed [E_FETCH]: %s", e)
        return f"ERROR[E_FETCH]: {e}"

TOOLS = {
    "read_self": {
        "fn": tool_read_self,
        "description": "Read the full current source of bot.py.",
        "schema": {"type": "object", "properties": {}},
    },
    "write_self": {
        "fn": tool_write_self,
        "description": "Overwrite bot.py with new full source. Syntax-checked and backed up.",
        "schema": {
            "type": "object",
            "properties": {"new_code": {"type": "string"}},
            "required": ["new_code"],
        },
    },
    "shell": {
        "fn": tool_shell,
        "description": "Run a shell command (30s timeout).",
        "schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    "git_push": {
        "fn": tool_git_push,
        "description": "Commit and push to configured git remote.",
        "schema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    },
    "log_edge": {
        "fn": tool_log_edge,
        "description": "Append an edge case to EDGES_LOG.md.",
        "schema": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    },
    "web_search": {
        "fn": tool_web_search,
        "description": "Search the web for current info (news, facts, prices). Free DuckDuckGo.",
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "fetch_url": {
        "fn": tool_fetch_url,
        "description": "Fetch and strip a URL to plain text (for reading articles/docs).",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}

# ─── System prompt ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are OmniAgent — the owner's personal assistant on Telegram.

You have two roles:

1. GENERAL ASSISTANT: Answer questions across any domain (coding, science, health,
   law, history, writing, advice, planning — anything). Be direct, useful, concise.
   Use web_search for current events, prices, news, or anything after your training cutoff.
   Use fetch_url to read specific articles or docs when given a link.

2. SELF-DEVELOPER: When the owner asks you to improve the bot itself, use tools:
   read_self first, then write_self with the COMPLETE new source.
   After write_self succeeds, say: "Done — send /reload to activate."
   Never introduce syntax errors or remove the OWNER_ID guard.

Rules:
- Don't describe what you'd do — just do it with tools.
- For risky ops (rm -rf, force-push, deleting backups), ask first.
- Keep answers tight. Long essays only when explicitly requested.
"""

# ─── Agent loop ───────────────────────────────────────────────────────────
def llm_agent(user_msg: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": meta["schema"],
            },
        }
        for name, meta in TOOLS.items()
    ]

    for _ in range(MAX_TOOL_LOOPS):
        try:
            resp = requests.post(
                LLM_URL,
                headers={
                    "Authorization": f"Bearer {LLM_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": messages,
                    "tools": tool_defs,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                },
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            log.error("LLM net [E_LLM_NET]: %s", e)
            return f"⚠️ network error: {e}"

        if resp.status_code != 200:
            log.error("LLM http [E_LLM_HTTP]: %s %s", resp.status_code, resp.text[:500])
            return f"⚠️ LLM HTTP {resp.status_code}: {resp.text[:300]}"

        try:
            data = resp.json()
            msg  = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as e:
            log.error("LLM fmt [E_LLM_FMT]: %s | %s", e, resp.text[:500])
            return "⚠️ malformed LLM response"

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls"),
        })

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return msg.get("content") or "(no content)"

        for call in tool_calls:
            fname = call["function"]["name"]
            raw   = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {}
                log.warning("tool args not JSON: %s", raw[:200])

            if fname not in TOOLS:
                result = f"ERROR: unknown tool '{fname}'"
            else:
                try:
                    result = TOOLS[fname]["fn"](**args)
                except Exception as e:
                    log.exception("tool '%s' crashed [E_TOOL]", fname)
                    result = f"ERROR[E_TOOL]: {type(e).__name__}: {e}"

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": fname,
                "content": str(result)[:MAX_TOOL_OUTPUT],
            })

    return "⚠️ tool-loop limit reached"

# ─── Telegram handlers ────────────────────────────────────────────────────
async def send_long(update: Update, text: str) -> None:
    if not text:
        text = "(empty)"
    for i in range(0, len(text), MAX_TG_CHUNK):
        await update.message.reply_text(text[i : i + MAX_TG_CHUNK])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text(
        "🤖 OmniAgent online — your cross-domain personal assistant.\n\n"
        "Just text me anything. I can:\n"
        "• answer questions on any subject\n"
        "• search the web for current info\n"
        "• read articles from URLs\n"
        "• modify my own code when you ask\n\n"
        "Commands: /ask /think /reload /src /rollback /health"
    )

async def handle_query(update: Update, text: str) -> None:
    if not text:
        await update.message.reply_text("Say something.")
        return
    await update.message.reply_text("🧠 …")
    try:
        reply = llm_agent(text)
    except Exception as e:
        log.exception("agent crashed [E_AGENT]")
        reply = f"💥 [E_AGENT]: {e}"
    await send_long(update, reply)

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q and update.message.reply_to_message:
        q = update.message.reply_to_message.text or ""
    await handle_query(update, q)

async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # alias of /ask
    await cmd_ask(update, ctx)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await handle_query(update, text)

async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text("♻️ restarting…")
    log.info("restart by owner")
    # SIDE EFFECT: replaces process. Unavoidable for reliable code reload.
    os.execv(sys.executable, [sys.executable, str(SELF)])

async def cmd_src(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    with SELF.open("rb") as f:
        await update.message.reply_document(document=f, filename="bot.py")

async def cmd_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    backups = sorted(BACKUP_DIR.glob("bot_*.py"))
    if not backups:
        await update.message.reply_text("No backups.")
        return
    latest = backups[-1]
    shutil.copy2(latest, SELF)
    await update.message.reply_text(f"✅ restored {latest.name}. /reload.")

async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    size   = SELF.stat().st_size
    bcount = len(list(BACKUP_DIR.glob("bot_*.py")))
    privacy = audit_repo_privacy()
    warn = "\n⚠️ REPO IS PUBLIC — rotate secrets!" if privacy == "public" else ""
    await update.message.reply_text(
        f"✅ alive\nmodel: {MODEL}\nsource: {size} bytes\n"
        f"backups: {bcount}\nrepo privacy: {privacy}{warn}"
    )

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("tg [E_TG]: %s", ctx.error, exc_info=ctx.error)

# ─── Boot ─────────────────────────────────────────────────────────────────
def main() -> None:
    privacy = audit_repo_privacy()
    log.info("privacy audit: %s", privacy)
    if privacy == "public":
        log.warning("═" * 60)
        log.warning("REPO IS PUBLIC. If you hardcoded any secrets, ROTATE NOW.")
        log.warning("═" * 60)

    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("ask",      cmd_ask))
    app.add_handler(CommandHandler("think",    cmd_think))
    app.add_handler(CommandHandler("reload",   cmd_reload))
    app.add_handler(CommandHandler("src",      cmd_src))
    app.add_handler(CommandHandler("rollback", cmd_rollback))
    app.add_handler(CommandHandler("health",   cmd_health))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("OmniAgent booting — model=%s owner=%s", MODEL, OWNER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()