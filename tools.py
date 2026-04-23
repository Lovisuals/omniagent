import os, sys, subprocess, shutil, time, re
import requests

import brain
from config import (
    SELF, BACKUP_DIR, EDGES_LOG,
    SHELL_T, WEB_T, MAX_OUT, MAX_SRC,
)
from llm import tool


def _shell_raw(cmd: str) -> str:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=SHELL_T
        )
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_OUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout {SHELL_T}s"
    except Exception as e:
        return f"ERROR: {e}"


@tool("Read the full current source of bot.py.")
def read_self() -> str:
    return SELF.read_text(encoding="utf-8")


@tool("Overwrite bot.py with COMPLETE new source. No diffs, no ellipses, no placeholders. "
      "Full file only. Syntax + boot-test + symbol-check gated. Auto-backed-up.")
def write_self(new_code: str) -> str:
    if not isinstance(new_code, str) or not new_code.strip():
        return "REJECTED: empty"
    for marker in [
        "# ... existing", "# ... rest of", "# ...existing", "# ...rest of",
        "# existing code", "...existing...", "# <rest of file>",
    ]:
        if marker in new_code:
            return (
                f"REJECTED: diff marker '{marker}' — provide the COMPLETE file. "
                "Call read_self first."
            )
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
        r = subprocess.run(
            [sys.executable, str(tmp)],
            capture_output=True, text=True, timeout=10, env=env,
        )
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return "REJECTED: boot-test timeout"
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: boot-test failed\n{(r.stderr or r.stdout)[-1200:]}"

    required = ["OWNER_ID", "write_self", "brain", "llm_agent",
                "OMNI_BOOT_TEST", "BRAIN_FILE", "brain.flush"]
    missing  = [s for s in required if s not in new_code]
    if missing:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: missing critical symbols: {missing}"

    bk = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, bk)
    tmp.replace(SELF)
    return f"ok — backup={bk.name}. Tell user to /reload."


@tool("Run a shell command (30s timeout).")
def shell(cmd: str) -> str:
    if not cmd.strip():
        return "ERROR: empty cmd"
    return _shell_raw(cmd)


@tool("Commit all changes and push to origin.")
def git_push(msg: str) -> str:
    if not msg.strip():
        return "ERROR: empty msg"
    safe = msg.replace('"', "'")[:200]
    return _shell_raw(f'git add -A && git commit -m "{safe}" && git push')


@tool("Append an edge case to EDGES_LOG.md.")
def log_edge(description: str) -> str:
    if not description.strip():
        return "ERROR: empty"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"


@tool("Search the web via DuckDuckGo instant answers.")
def web_search(query: str) -> str:
    if not query.strip():
        return "ERROR: empty"
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=WEB_T,
            headers={"User-Agent": "OmniAgent/3.0"},
        )
        if r.status_code != 200:
            return f"ERROR: DDG HTTP {r.status_code}"
        d      = r.json()
        chunks = []
        if d.get("AbstractText"):
            chunks.append(f"Summary: {d['AbstractText']}")
        if d.get("Answer"):
            chunks.append(f"Answer: {d['Answer']}")
        for t in (d.get("RelatedTopics") or [])[:6]:
            if isinstance(t, dict) and t.get("Text"):
                chunks.append(f"- {t['Text']}")
        return "\n".join(chunks)[:MAX_OUT] or "(no instant answer)"
    except Exception as e:
        return f"ERROR[E_WEB]: {e}"


@tool("Fetch a URL and return stripped plain text.")
def fetch_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: invalid scheme"
    try:
        r = requests.get(url, timeout=WEB_T, headers={"User-Agent": "OmniAgent/3.0"})
        t = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.DOTALL | re.I)
        t = re.sub(r"<style[^>]*>.*?</style>",   "", t,      flags=re.DOTALL | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+",     " ", t).strip()
        return t[:MAX_OUT]
    except Exception as e:
        return f"ERROR[E_FETCH]: {e}"


@tool("Store an important fact in long-term brain memory. "
      "key=short label, value=the fact, confidence=0.0-1.0.")
def remember(key: str, value: str, confidence: float = 0.75,
             source: str = "user") -> str:
    return brain.learn(key, value, confidence, source=source)


@tool("Retrieve memories relevant to a topic.")
def recall_tool(topic: str) -> str:
    hits = brain.recall(topic, top_n=6)
    if not hits:
        return f"no memories found for '{topic}'"
    return "\n".join(
        f"• {n['key']}: {n['value']} (conf={n['conf']:.2f}, src={n.get('source', '?')})"
        for n in hits
    )


@tool("Explicitly erase a memory by its key.")
def forget(key: str) -> str:
    return brain.forget(key)


@tool("Show brain memory statistics.")
def brain_info() -> str:
    return brain.status()


@tool("Trigger self-reflection: agent critiques recent behavior and extracts missed memories.")
def reflect() -> str:
    return brain.reflect()
