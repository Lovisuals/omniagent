import os, sys, shutil, logging, time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
)

import brain
import tools
from llm import llm_agent, TOOLS
from config import (
    TG_TOKEN, OWNER_ID, ALLOWED_USERS, MODEL, FALLBACK_MODEL,
    SELF, BACKUP_DIR, GITHUB_REPO, GITHUB_TOKEN,
    MAX_CHUNK, CONSOLIDATE_EVERY, DECAY_EVERY,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


def is_owner(u: Update) -> bool:
    return bool(u.effective_user and u.effective_user.id == OWNER_ID)


def is_authorized(u: Update) -> bool:
    uid = u.effective_user.id if u.effective_user else None
    return uid == OWNER_ID or uid in ALLOWED_USERS


def user_ctx(u: Update) -> str:
    uid = u.effective_user.id if u.effective_user else OWNER_ID
    return "" if uid == OWNER_ID else f"user_{uid}"


def audit_repo_privacy() -> str:
    if not GITHUB_REPO:
        return "unconfigured"
    try:
        import requests
        h = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}", headers=h, timeout=10
        )
        if r.status_code == 200:
            return "private" if r.json().get("private") else "public"
        return "unknown"
    except Exception:
        return "unknown"


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
        reply = llm_agent(text, user_id=user_ctx(update))
    except Exception as e:
        log.exception("E_AGENT")
        reply = f"💥 {e}"
    await send_long(update, reply)


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u):
        return
    role  = "owner" if is_owner(u) else "guest"
    count = brain.meta().get("interact_count", 0)
    next_c = CONSOLIDATE_EVERY - (count % CONSOLIDATE_EVERY or CONSOLIDATE_EVERY)
    await u.message.reply_text(
        f"🤖 OmniAgent v3 [{role}]\n"
        f"{brain.status()}\n\n"
        "I remember. I learn. I forget what fades. I reflect. I evolve.\n\n"
        f"Next consolidation in {next_c} interactions.\n\n"
        "/brain [save|backup]\n"
        "/remember key :: value\n"
        "/forget key\n"
        "/reflect\n"
        "/reflections\n"
        "/consolidate\n"
        "/health /src /rollback /tools /model"
    )


async def cmd_ask(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u):
        return
    q = " ".join(c.args).strip()
    if not q and u.message.reply_to_message:
        q = u.message.reply_to_message.text or ""
    await handle_query(u, q)


async def cmd_think(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_ask(u, c)


async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u):
        return
    t = (u.message.text or "").strip()
    if t and not t.startswith("/"):
        await handle_query(u, t)


async def cmd_reload(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    brain.flush(force=True)
    await u.message.reply_text("♻️ flushing brain + restarting…")
    os.execv(sys.executable, [sys.executable, str(SELF)])


async def cmd_src(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    with SELF​​​​​​​​​​​​​​​​
