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
    role   = "owner" if is_owner(u) else "guest"
    count  = brain.meta().get("interact_count", 0)
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
    with SELF.open("rb") as f:
        await u.message.reply_document(document=f, filename="bot.py")


async def cmd_rollback(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    backups = sorted(BACKUP_DIR.glob("bot_*.py"))
    if not backups:
        await u.message.reply_text("No backups.")
        return
    shutil.copy2(backups[-1], SELF)
    await u.message.reply_text(f"✅ restored {backups[-1].name}. /reload.")


async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    priv   = audit_repo_privacy()
    warn   = "\n⚠️ REPO IS PUBLIC — rotate secrets" if priv == "public" else ""
    m      = brain.meta()
    count  = m.get("interact_count", 0)
    next_c = CONSOLIDATE_EVERY - (count % CONSOLIDATE_EVERY or CONSOLIDATE_EVERY)
    next_d = DECAY_EVERY - (count % DECAY_EVERY or DECAY_EVERY)
    rlog   = brain.reflection_log()
    last_r = f"\"{rlog[-1]['summary'][:80]}\"" if rlog else "none"
    last_commit = m.get("last_commit", 0)
    last_c_str  = (
        time.strftime("%m/%d %H:%M", time.localtime(last_commit))
        if last_commit else "never"
    )
    await u.message.reply_text(
        f"✅ OmniAgent v3\n"
        f"model: {MODEL} | fallback: {FALLBACK_MODEL}\n"
        f"source: {SELF.stat().st_size}b | "
        f"backups: {len(list(BACKUP_DIR.glob('bot_*.py')))}\n"
        f"tools: {len(TOOLS)} | privacy: {priv}{warn}\n"
        f"interactions: {count} | allowed_users: {len(ALLOWED_USERS)}\n"
        f"next consolidation: {next_c} | next decay: {next_d}\n"
        f"last brain commit: {last_c_str}\n"
        f"last reflection: {last_r}\n"
        f"{brain.status()}"
    )


async def cmd_tools(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    lines = [f"• {n} — {m['description']}" for n, m in TOOLS.items()]
    await u.message.reply_text("🛠 Tools:\n" + "\n".join(lines))


async def cmd_model(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    await u.message.reply_text(
        f"primary:  {MODEL}\n"
        f"fallback: {FALLBACK_MODEL}\n"
        "(override via MODEL / FALLBACK_MODEL env vars)"
    )


async def cmd_brain(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    arg = " ".join(c.args).strip().lower()

    if arg == "save":
        brain.flush(force=True)
        result = brain.commit()
        await u.message.reply_text(f"🧠 saved + committed\n{result[:200]}")
        return

    if arg == "backup":
        await u.message.reply_text(f"🧠 {brain.backup()}")
        return

    nodes = brain._nodes()
    if not nodes:
        await u.message.reply_text("🧠 Brain is empty. Start talking.")
        return

    lines = []
    for n in sorted(nodes.values(), key=lambda x: x["conf"], reverse=True):
        age_h = (time.time() - n["ts"]) / 3600
        ctx_s = f" [{n['ctx']}]" if n.get("ctx") else ""
        lines.append(
            f"[{n['conf']:.2f}] {n['key']}: {n['value'][:80]}  "
            f"(age:{age_h:.0f}h src:{n.get('source', '?')}{ctx_s})"
        )
    await send_long(u, f"🧠 Brain v3 — {len(nodes)} nodes\n\n" + "\n".join(lines))


async def cmd_remember(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u):
        return
    args = " ".join(c.args).strip()
    if "::" not in args:
        await u.message.reply_text("Usage: /remember key :: value")
        return
    key, _, val = args.partition("::")
    ctx = user_ctx(u)
    await u.message.reply_text(
        f"🧠 {brain.learn(key.strip(), val.strip(), ctx=ctx, source='manual')}"
    )


async def cmd_forget(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u):
        return
    key = " ".join(c.args).strip()
    if not key:
        await u.message.reply_text("Usage: /forget key")
        return
    await u.message.reply_text(f"🧠 {brain.forget(key)}")


async def cmd_consolidate(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    await u.message.reply_text("🧠 consolidating…")
    await send_long(u, f"🧠 {brain.consolidate(do_commit=True)}")


async def cmd_reflect(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    await u.message.reply_text("🧠 reflecting…")
    await send_long(u, f"🧠 {brain.reflect()}")


async def cmd_reflection_log(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u):
        return
    rlog = brain.reflection_log()
    if not rlog:
        await u.message.reply_text("🧠 No reflections yet. /reflect to start.")
        return
    lines = []
    for entry in reversed(rlog[-5:]):
        ts = time.strftime("%m/%d %H:%M", time.localtime(entry["ts"]))
        lines.append(f"[{ts}] {entry['summary']}")
        for a in entry.get("actions", []):
            lines.append(f"  → {a}")
    await send_long(u, "🧠 Recent reflections:\n\n" + "\n".join(lines))


async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("E_TG %s", c.error, exc_info=c.error)


def main() -> None:
    if os.environ.get("OMNI_BOOT_TEST") == "1":
        brain.load()
        assert callable(llm_agent),    "llm_agent missing"
        assert callable(brain.learn),  "brain.learn missing"
        assert callable(brain.flush),  "brain.flush missing"
        assert OWNER_ID,               "OWNER_ID missing"
        print("boot-test ok")
        sys.exit(0)

    priv = audit_repo_privacy()
    log.info(
        "privacy=%s tools=%d model=%s allowed_users=%d",
        priv, len(TOOLS), MODEL, len(ALLOWED_USERS),
    )
    if priv == "public":
        log.warning("REPO IS PUBLIC — rotate secrets immediately")

    brain.load()

    app = ApplicationBuilder().token(TG_TOKEN).build()
    for name, fn in [
        ("start",       cmd_start),
        ("ask",         cmd_ask),
        ("think",       cmd_think),
        ("reload",      cmd_reload),
        ("src",         cmd_src),
        ("rollback",    cmd_rollback),
        ("health",      cmd_health),
        ("tools",       cmd_tools),
        ("model",       cmd_model),
        ("brain",       cmd_brain),
        ("remember",    cmd_remember),
        ("forget",      cmd_forget),
        ("consolidate", cmd_consolidate),
        ("reflect",     cmd_reflect),
        ("reflections", cmd_reflection_log),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("OmniAgent v3 — up. model=%s owner=%s", MODEL, OWNER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
