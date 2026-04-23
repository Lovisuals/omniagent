import os, sys, shutil, logging, time, requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import brain, omni_tools
from llm import llm_agent, TOOLS
from config import (
    TG_TOKEN, OWNER_ID, ALLOWED_USERS, MODEL, FALLBACK_MODEL,
    SELF, BACKUP_DIR, GITHUB_REPO, GITHUB_TOKEN,
    MAX_CHUNK, CONSOLIDATE_EVERY, DECAY_EVERY,
)
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")
def is_owner(u: Update) -> bool: return bool(u.effective_user and u.effective_user.id == OWNER_ID)
def is_authorized(u: Update) -> bool:
    uid = u.effective_user.id if u.effective_user else None
    return uid == OWNER_ID or uid in ALLOWED_USERS
def user_ctx(u: Update) -> str:
    uid = u.effective_user.id if u.effective_user else OWNER_ID
    return "" if uid == OWNER_ID else f"user_{uid}"
def audit_repo_privacy() -> str:
    if not GITHUB_REPO: return "unconfigured"
    try:
        h = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN: h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}", headers=h, timeout=10)
        if r.status_code == 200: return "private" if r.json().get("private") else "public"
        return "unknown"
    except: return "unknown"
async def send_long(update: Update, text: str) -> None:
    text = text or "(empty)"
    for i in range(0, len(text), MAX_CHUNK): await update.message.reply_text(text[i:i + MAX_CHUNK])
async def handle_query(update: Update, text: str) -> None:
    if not text: await update.message.reply_text("Say something."); return
    await update.message.reply_text("🧠 …")
    try: reply = llm_agent(text, user_id=user_ctx(update))
    except Exception as e: log.exception("E_AGENT"); reply = f"💥 {e}"
    await send_long(update, reply)
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    r, cnt = ("owner" if is_owner(u) else "guest"), brain.meta().get("interact_count", 0)
    nxt = CONSOLIDATE_EVERY - (cnt % CONSOLIDATE_EVERY or CONSOLIDATE_EVERY)
    await u.message.reply_text(f"🤖 OmniAgent v3 [{r}]\n{brain.status()}\n\nI remember. I learn. I forget what fades. I reflect. I evolve.\n\nNext consolidation in {nxt} interactions.\n\n/brain [save|backup]\n/remember key :: value\n/forget key\n/reflect\n/reflections\n/consolidate\n/health /src /rollback /tools /model")
async def cmd_ask(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    q = " ".join(c.args).strip()
    if not q and u.message.reply_to_message: q = u.message.reply_to_message.text or ""
    await handle_query(u, q)
async def cmd_think(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None: await cmd_ask(u, c)
async def on_text(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u) or not u.message.text: return
    t = u.message.text.strip()
    if t and not t.startswith("/"): await handle_query(u, t)
async def cmd_reload(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    brain.flush(force=True); await u.message.reply_text("♻️ restarting…"); os.execv(sys.executable, [sys.executable, str(SELF)])
async def cmd_src(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    with SELF.open("rb") as f: await u.message.reply_document(document=f, filename="bot.py")
async def cmd_rollback(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    bks = sorted(BACKUP_DIR.glob("bot_*.py"))
    if not bks: await u.message.reply_text("No backups."); return
    shutil.copy2(bks[-1], SELF); await u.message.reply_text(f"✅ restored {bks[-1].name}. /reload.")
async def cmd_health(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    priv, m = audit_repo_privacy(), brain.meta(); warn = "\n⚠️ PUBLIC REPO" if priv == "public" else ""
    cnt, rlog = m.get("interact_count", 0), brain.reflection_log()
    nxt_c, nxt_d = CONSOLIDATE_EVERY - (cnt % CONSOLIDATE_EVERY or CONSOLIDATE_EVERY), DECAY_EVERY - (cnt % DECAY_EVERY or DECAY_EVERY)
    lc = time.strftime("%m/%d %H:%M", time.localtime(m.get("last_commit", 0))) if m.get("last_commit") else "never"
    await u.message.reply_text(f"✅ OmniAgent v3\nmodel: {MODEL}\nsource: {SELF.stat().st_size}b | backups: {len(list(BACKUP_DIR.glob('bot_*.py')))}\ntools: {len(TOOLS)} | privacy: {priv}{warn}\ninteractions: {cnt}\nnext: C:{nxt_c} D:{nxt_d} | commit: {lc}\n{brain.status()}")
async def cmd_tools(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("🛠 Tools:\n" + "\n".join(f"• {n} — {m['description']}" for n, m in TOOLS.items()))
async def cmd_model(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text(f"primary: {MODEL}\nfallback: {FALLBACK_MODEL}")
async def cmd_brain(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    arg = " ".join(c.args).strip().lower()
    if arg == "save": brain.flush(force=True); res = brain.commit(); await u.message.reply_text(f"🧠 saved\n{res[:200]}"); return
    if arg == "backup": await u.message.reply_text(f"🧠 {brain.backup()}"); return
    nodes = brain._nodes()
    if not nodes: await u.message.reply_text("🧠 Empty."); return
    lines = [f"[{n['conf']:.2f}] {n['key']}: {n['value'][:80]} (age:{int((time.time()-n['ts'])/3600)}h)" for n in sorted(nodes.values(), key=lambda x: x["conf"], reverse=True)]
    await send_long(u, f"🧠 Brain v3 — {len(nodes)} nodes\n\n" + "\n".join(lines))
async def cmd_remember(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    args = " ".join(c.args).strip()
    if "::" not in args: await u.message.reply_text("Usage: /remember k :: v"); return
    k, _, v = args.partition("::"); await u.message.reply_text(f"🧠 {brain.learn(k.strip(), v.strip(), ctx=user_ctx(u), source='manual')}")
async def cmd_forget(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(u): return
    k = " ".join(c.args).strip(); await u.message.reply_text(f"🧠 {brain.forget(k)}" if k else "Usage: /forget k")
async def cmd_consolidate(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("🧠 consolidating…"); await send_long(u, f"🧠 {brain.consolidate()}")
async def cmd_reflect(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    await u.message.reply_text("🧠 reflecting…"); await send_long(u, f"🧠 {brain.reflect()}")
async def cmd_reflection_log(u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(u): return
    rlog = brain.reflection_log()
    if not rlog: await u.message.reply_text("🧠 No reflections."); return
    await send_long(u, "🧠 Recent reflections:\n\n" + "\n".join(f"[{time.strftime('%m/%d %H:%M', time.localtime(e['ts']))}] {e['summary']}" for e in reversed(rlog[-5:])))
async def on_error(u: object, c: ContextTypes.DEFAULT_TYPE) -> None:
    if "Conflict" in str(c.error): return
    log.error("E_TG %s", c.error)
def main() -> None:
    if os.environ.get("OMNI_BOOT_TEST") == "1":
        brain.load(); assert callable(llm_agent); assert OWNER_ID; print("boot-test ok"); sys.exit(0)
    brain.load(); time.sleep(4)
    app = ApplicationBuilder().token(TG_TOKEN).build()
    for n, f in [("start", cmd_start), ("ask", cmd_ask), ("think", cmd_think), ("reload", cmd_reload), ("src", cmd_src), ("rollback", cmd_rollback), ("health", cmd_health), ("tools", cmd_tools), ("model", cmd_model), ("brain", cmd_brain), ("remember", cmd_remember), ("forget", cmd_forget), ("consolidate", cmd_consolidate), ("reflect", cmd_reflect), ("reflections", cmd_reflection_log)]: app.add_handler(CommandHandler(n, f))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error); log.info("OmniAgent v3 — up."); app.run_polling(drop_pending_updates=True)
if __name__ == "__main__": main()