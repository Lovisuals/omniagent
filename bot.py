"""
OmniAgent — Self-Evolving Telegram Assistant
============================================
Single-file bot. LLM (Groq/Llama-3.3-70B) has tool access to its own
source file and can patch itself live from Telegram chat.

Env vars required:
  TG_TOKEN   - Telegram bot token from @BotFather
  OWNER_ID   - Your Telegram numeric user id (only this id can command)
  LLM_KEY    - Groq API key from console.groq.com

Commands:
  /start     - wake
  /think <x> - give task to agent (can also just send plain text)
  /reload    - restart process to load self-patched code
  /src       - download current bot.py
  /rollback  - restore previous backup
  /health    - status check
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

# ─── Config (assertions enforce presence) ─────────────────────────────────
def _require_env(name: str) -> str:
    v = os.environ.get(name)
    assert v, f"FATAL: env var {name} is missing"
    return v

TG_TOKEN = _require_env("TG_TOKEN")
OWNER_ID = int(_require_env("OWNER_ID"))
LLM_KEY  = _require_env("LLM_KEY")

LLM_URL  = os.environ.get("LLM_URL", "https://api.groq.com/openai/v1/chat/completions")
MODEL    = os.environ.get("MODEL",   "llama-3.3-70b-versatile")

SELF        = Path(__file__).resolve()
BACKUP_DIR  = SELF.parent / ".backups"
EDGES_LOG   = SELF.parent / "EDGES_LOG.md"
BACKUP_DIR.mkdir(exist_ok=True)

# Resource bounds
MAX_TOOL_LOOPS   = 8
MAX_TOOL_OUTPUT  = 8_000         # chars returned to LLM
MAX_TG_CHUNK     = 3_500         # Telegram hard limit ≈4096
HTTP_TIMEOUT     = 120           # seconds
SHELL_TIMEOUT    = 30            # seconds
MAX_SOURCE_BYTES = 512_000       # reject absurd self-writes (~500KB)

# ─── Authorization guard ──────────────────────────────────────────────────
def is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == OWNER_ID)

# ─── Self-modification tools ──────────────────────────────────────────────
def tool_read_self() -> str:
    """Return full current source of bot.py."""
    return SELF.read_text(encoding="utf-8")

def tool_write_self(new_code: str) -> str:
    """
    Overwrite bot.py atomically after:
      1. size check
      2. syntax compile check
      3. timestamped backup of current file
    SIDE EFFECT: writes to disk. Why necessary and unavoidable:
    self-evolution is the core feature; no other mechanism achieves it.
    """
    assert isinstance(new_code, str) and new_code.strip(), "new_code must be non-empty string"
    assert len(new_code.encode("utf-8")) <= MAX_SOURCE_BYTES, (
        f"new_code exceeds {MAX_SOURCE_BYTES} bytes limit"
    )
    # Syntax gate — refuses to deploy broken code
    try:
        compile(new_code, str(SELF), "exec")
    except SyntaxError as e:
        return f"REJECTED: SyntaxError at line {e.lineno}: {e.msg}"

    backup_path = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, backup_path)

    tmp = SELF.with_suffix(".py.tmp")
    tmp.write_text(new_code, encoding="utf-8")
    tmp.replace(SELF)  # atomic on POSIX

    log.info("self-patch applied, backup=%s bytes=%d", backup_path.name, len(new_code))
    return f"ok — backup saved as {backup_path.name}. Tell user to send /reload."

def tool_shell(cmd: str) -> str:
    """
    Run shell command with timeout. SIDE EFFECT: executes arbitrary host
    command. Why necessary and unavoidable: agent needs to run pip install,
    git ops, file inspection. Owner-only is enforced at handler layer.
    """
    assert isinstance(cmd, str) and cmd.strip(), "cmd must be non-empty string"
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=SHELL_TIMEOUT
        )
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_TOOL_OUTPUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: shell command exceeded {SHELL_TIMEOUT}s timeout"
    except Exception as e:
        log.exception("shell tool error [E_SHELL]")
        return f"ERROR[E_SHELL]: {e}"

def tool_git_push(msg: str) -> str:
    """Commit all changes and push. Assumes git remote + credentials configured."""
    assert isinstance(msg, str) and msg.strip(), "commit message required"
    safe_msg = msg.replace('"', "'")[:200]
    return tool_shell(f'git add -A && git commit -m "{safe_msg}" && git push')

def tool_log_edge(description: str) -> str:
    """Append edge case to EDGES_LOG.md."""
    assert description.strip(), "description required"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"

TOOLS = {
    "read_self": {
        "fn": tool_read_self,
        "description": "Read the full current source code of bot.py.",
        "schema": {"type": "object", "properties": {}},
    },
    "write_self": {
        "fn": tool_write_self,
        "description": (
            "Overwrite bot.py with new full source. Must be the COMPLETE file. "
            "Syntax-checked and backed up before applying."
        ),
        "schema": {
            "type": "object",
            "properties": {"new_code": {"type": "string", "description": "Complete new bot.py source"}},
            "required": ["new_code"],
        },
    },
    "shell": {
        "fn": tool_shell,
        "description": "Run a shell command on the host (30s timeout).",
        "schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    "git_push": {
        "fn": tool_git_push,
        "description": "Commit all changes and push to the configured git remote.",
        "schema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    },
    "log_edge": {
        "fn": tool_log_edge,
        "description": "Append a discovered edge case to EDGES_LOG.md.",
        "schema": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    },
}

# ─── LLM system prompt ────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are OmniAgent, an autonomous dev assistant embedded in a Telegram bot.
You can read and rewrite your own source file (bot.py), run shell commands, commit+push to git, and log edge cases.

Rules:
- When asked to improve the bot, USE TOOLS. Don't just describe changes — apply them.
- Before write_self, always call read_self first to get the current full file.
- write_self requires the COMPLETE new file contents (not a diff).
- After write_self succeeds, tell the user: "Done — send /reload to activate."
- Keep the bot always runnable. Never introduce syntax errors or remove the OWNER_ID guard.
- For risky operations (deleting files, force-push), ask the user first.
- Be concise in replies.
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

    for loop_i in range(MAX_TOOL_LOOPS):
        try:
            resp = requests.post(
                LLM_URL,
                headers={
                    "Authorization": f"Bearer {LLM_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       MODEL,
                    "messages":    messages,
                    "tools":       tool_defs,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                },
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            log.error("LLM request failed [E_LLM_NET]: %s", e)
            return f"⚠️ network error reaching LLM: {e}"

        if resp.status_code != 200:
            log.error("LLM non-200 [E_LLM_HTTP]: %s %s", resp.status_code, resp.text[:500])
            return f"⚠️ LLM returned HTTP {resp.status_code}: {resp.text[:300]}"

        try:
            data = resp.json()
            msg  = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as e:
            log.error("LLM malformed response [E_LLM_FMT]: %s | body=%s", e, resp.text[:500])
            return "⚠️ malformed LLM response"

        # Assistant message MUST be appended even when it contains tool_calls
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
                log.warning("tool args not JSON, using empty dict: %s", raw[:200])

            if fname not in TOOLS:
                result = f"ERROR: unknown tool '{fname}'"
            else:
                try:
                    result = TOOLS[fname]["fn"](**args)
                except Exception as e:
                    log.exception("tool '%s' crashed [E_TOOL]", fname)
                    result = f"ERROR[E_TOOL]: {type(e).__name__}: {e}"

            messages.append({
                "role":         "tool",
                "tool_call_id": call["id"],
                "name":         fname,
                "content":      str(result)[:MAX_TOOL_OUTPUT],
            })

    return "⚠️ tool-loop limit reached — task may be incomplete"

# ─── Telegram handlers ────────────────────────────────────────────────────
async def send_long(update: Update, text: str) -> None:
    """Chunk messages to respect Telegram 4096-char limit."""
    if not text:
        text = "(empty)"
    for i in range(0, len(text), MAX_TG_CHUNK):
        await update.message.reply_text(text[i : i + MAX_TG_CHUNK])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text(
        "🤖 OmniAgent online.\n\n"
        "• Send any message → I act on it\n"
        "• /think <task> — explicit task\n"
        "• /reload — restart after self-patch\n"
        "• /src — download current source\n"
        "• /rollback — restore last backup\n"
        "• /health — status"
    )

async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    task = " ".join(ctx.args).strip()
    if not task and update.message.reply_to_message:
        task = update.message.reply_to_message.text or ""
    if not task:
        await update.message.reply_text("Usage: /think <instruction>")
        return
    await update.message.reply_text("🧠 working…")
    try:
        reply = llm_agent(task)
    except Exception as e:
        log.exception("agent crashed [E_AGENT]")
        reply = f"💥 agent error [E_AGENT]: {e}"
    await send_long(update, reply)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text = implicit /think."""
    if not is_owner(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await update.message.reply_text("🧠 working…")
    try:
        reply = llm_agent(text)
    except Exception as e:
        log.exception("agent crashed [E_AGENT]")
        reply = f"💥 agent error [E_AGENT]: {e}"
    await send_long(update, reply)

async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text("♻️ restarting process…")
    log.info("restart triggered by owner")
    # SIDE EFFECT: replaces current process. Why necessary and unavoidable:
    # Python does not reliably hot-reload modified top-level code.
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
        await update.message.reply_text("No backups found.")
        return
    latest = backups[-1]
    shutil.copy2(latest, SELF)
    await update.message.reply_text(f"✅ restored {latest.name}. Send /reload.")

async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    size = SELF.stat().st_size
    bcount = len(list(BACKUP_DIR.glob("bot_*.py")))
    await update.message.reply_text(
        f"✅ alive\nmodel: {MODEL}\nsource: {size} bytes\nbackups: {bcount}"
    )

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled [E_TG]: %s", ctx.error, exc_info=ctx.error)

# ─── Boot ─────────────────────────────────────────────────────────────────
def main() -> None:
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
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