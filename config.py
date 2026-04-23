import os
from pathlib import Path

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

ALLOWED_USERS: set[int] = set()
_raw = os.environ.get("ALLOWED_USERS", "")
if _raw:
    for u in _raw.split(","):
        try:
            ALLOWED_USERS.add(int(u.strip()))
        except ValueError:
            pass

# Paths (relative to project root)
ROOT       = Path(__file__).resolve().parent
SELF       = ROOT / "bot.py"
BACKUP_DIR = ROOT / ".backups"
EDGES_LOG  = ROOT / "EDGES_LOG.md"
BRAIN_FILE = ROOT / "brain.json"

BACKUP_DIR.mkdir(exist_ok=True)

# Constants
MAX_LOOPS         = 12
MAX_OUT           = 8000
MAX_CHUNK         = 3500
HTTP_T            = 120
SHELL_T           = 30
WEB_T             = 15
MAX_SRC           = 512_000
CONSOLIDATE_EVERY = 50
DECAY_EVERY       = 20
FLUSH_INTERVAL    = 3.0

CONF_DECAY   = 0.06
CONF_FLOOR   = 0.15
CONF_BOOST   = 0.10
CONF_DEFAULT = 0.75