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