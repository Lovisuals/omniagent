import os, sys, subprocess, shutil, time, re
from pathlib import Path

import requests

import brain
from config import (
    SELF, BACKUP_DIR, EDGES_LOG, ROOT,
    SHELL_T, WEB_T, MAX_OUT, MAX_SRC,
)
from llm import tool


def _shell_raw(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=SHELL_T)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[-MAX_OUT:] or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout {SHELL_T}s"
    except Exception as e:
        return f"ERROR: {e}"


@tool("Read the full current source of bot.py.")
def read_self() -> str:
    return SELF.read_text(encoding="utf-8")


@tool("Overwrite bot.py with COMPLETE new source. No diffs, no ellipses. Full file only. Syntax + boot-test gated.")
def write_self(new_code: str) -> str:
    if not isinstance(new_code, str) or not new_code.strip():
        return "REJECTED: empty"
    for marker in ["# ... existing", "# ... rest of", "# ...existing", "# ...rest of",
                   "# existing code", "...existing...", "# <rest of file>"]:
        if marker in new_code:
            return f"REJECTED: diff marker '{marker}'. Provide complete file. Call read_self first."
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
        r = subprocess.run([sys.executable, str(tmp)], capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return "REJECTED: boot-test timeout"
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: boot-test failed\n{(r.stderr or r.stdout)[-1200:]}"
    required = ["OWNER_ID", "write_self", "brain", "llm_agent", "OMNI_BOOT_TEST", "BRAIN_FILE"]
    missing = [s for s in required if s not in new_code]
    if missing:
        tmp.unlink(missing_ok=True)
        return f"REJECTED: missing symbols: {missing}"
    bk = BACKUP_DIR / f"bot_{int(time.time())}.py"
    shutil.copy2(SELF, bk)
    tmp.replace(SELF)
    return f"ok — backup={bk.name}. Tell user to /reload."


@tool("Clone a GitHub repository into a subdirectory.")
def clone_repo(repo_url: str, target_dir: str = "openclaw") -> str:
    if not repo_url.startswith(("https://github.com/", "git@github.com:")):
        return "ERROR: Only GitHub URLs allowed."
    target = ROOT / target_dir
    if target.exists():
        return f"ERROR: {target_dir} already exists."
    result = _shell_raw(f"git clone {repo_url} {target_dir}")
    if "fatal" in result.lower():
        return f"Clone failed: {result[:300]}"
    return f"Cloned {repo_url} into ./{target_dir}"


@tool("Read content of any file or list directory contents.")
def read_file(path: str) -> str:
    full = ROOT / path
    if not full.exists():
        return f"ERROR: {path} not found."
    try:
        if full.is_dir():
            items = [f"{'📁' if (full/i).is_dir() else '📄'} {i.name}" for i in sorted(full.iterdir())]
            return "\n".join(items) or "(empty)"
        return full.read_text(encoding="utf-8")
    except Exception as e:
        return f"ERROR: {e}"


@tool("Write content to any file inside the project root.")
def write_file(path: str, content: str) -> str:
    if not path or not content:
        return "ERROR: path and content required."
    full = ROOT / path
    if not str(full.resolve()).startswith(str(ROOT.resolve())):
        return "ERROR: Path must be inside project root."
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


@tool("List files and directories at given path.")
def list_dir(path: str = ".") -> str:
    target = ROOT / path
    if not target.exists():
        return f"ERROR: {path} does not exist."
    try:
        items = [f"{'📁' if (target/i).is_dir() else '📄'} {i.name}" for i in sorted(target.iterdir())]
        return "\n".join(items) or "(empty)"
    except Exception as e:
        return f"ERROR: {e}"


@tool("Run a shell command with 30s timeout.")
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


@tool("Append entry to EDGES_LOG.md.")
def log_edge(description: str) -> str:
    if not description.strip():
        return "ERROR: empty"
    with EDGES_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {description.strip()}\n")
    return "logged"


@tool("Search web via DuckDuckGo.")
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
        d = r.json()
        chunks = []
        if d.get("AbstractText"): chunks.append(f"Summary: {d['AbstractText']}")
        if d.get("Answer"): chunks.append(f"Answer: {d['Answer']}")
        for t in (d.get("RelatedTopics") or [])[:6]:
            if isinstance(t, dict) and t.get("Text"):
                chunks.append(f"- {t['Text']}")
        return "\n".join(chunks)[:MAX_OUT] or "(no answer)"
    except Exception as e:
        return f"ERROR[E_WEB]: {e}"


@tool("Fetch URL and return cleaned text.")
def fetch_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: invalid scheme"
    try:
        r = requests.get(url, timeout=WEB_T, headers={"User-Agent": "OmniAgent/3.0"})
        t = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.DOTALL | re.I)
        t = re.sub(r"<style[^>]*>.*?</style>", "", t, flags=re.DOTALL | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:MAX_OUT]
    except Exception as e:
        return f"ERROR[E_FETCH]: {e}"


@tool("Store fact in long-term memory.")
def remember(key: str, value: str, confidence: float = 0.75, source: str = "user") -> str:
    return brain.learn(key, value, confidence, source=source)


@tool("Retrieve relevant memories.")
def recall_tool(topic: str) -> str:
    hits = brain.recall(topic, top_n=6)
    if not hits:
        return f"no memories for '{topic}'"
    return "\n".join(f"• {n['key']}: {n['value']} (conf={n['conf']:.2f}, src={n.get('source','?')})" for n in hits)


@tool("Erase a memory by key.")
def forget(key: str) -> str:
    return brain.forget(key)


@tool("Show brain status.")
def brain_info() -> str:
    return brain.status()


@tool("Trigger self-reflection.")
def reflect() -> str:
    return brain.reflect()