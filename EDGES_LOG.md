# EDGES_LOG — OmniAgent

Living log of edge cases discovered and how they're handled.

- [init] Unauthorized Telegram user sends command → `is_owner()` returns False, handler exits silently (no reply leaks bot existence).
- [init] LLM returns message >4096 chars → `send_long()` chunks output at 3500-char boundary.
- [init] `write_self` receives syntactically invalid Python → `compile()` gate rejects with explicit error message; file untouched.
- [init] Shell command hangs → 30s subprocess timeout raises TimeoutExpired, returns clean error string.
- [init] LLM infinite tool-call loop → MAX_TOOL_LOOPS=8 ceiling, returns truncation warning.
- [init] Groq API network failure → requests.RequestException caught, returns network-error string (no crash).
- [init] LLM returns malformed JSON in tool args → `json.JSONDecodeError` caught, falls back to empty args with warning log.
- [init] `os.execv` reload on Windows — POSIX-only; use Linux host (Railway/Render/Fly) for reliable reload.
- [v1.1] General-domain query with no web-search need → LLM answers from training data directly; no tool call.
- [v1.1] Query needs current info (e.g. "Bitcoin price today") → LLM triggers web_search tool automatically.
- [v1.1] User pastes URL asking for summary → LLM calls fetch_url, strips HTML, summarizes within token limit.
- [v1.1] DuckDuckGo returns no instant answer → tool returns "(no instant answer)" string, LLM can retry or explain.
- [v1.1] fetch_url on non-http(s) scheme → assertion rejects before network call.
- [v1.1] Repo flipped to public accidentally → audit_repo_privacy() logs warning on boot and in /health output.
- [v1.1] GITHUB_REPO env var unset → audit returns "unconfigured", /health shows this state without erroring.
- [v1.1] GitHub API rate-limits anonymous privacy check → returns "unknown"; bot continues running.