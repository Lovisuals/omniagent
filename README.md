# OmniAgent

Self-evolving Telegram bot. Owner chats → LLM patches `bot.py` → `/reload` → new behavior live.

## Deploy

1. **BotFather**: `/newbot` → get `TG_TOKEN`
2. **Your Telegram ID**: message `@userinfobot` → copy numeric id
3. **Groq key**: https://console.groq.com/keys → create
4. **Railway** (or Render/Fly): New Project → Deploy from this repo → add env vars:
   - `TG_TOKEN`
   - `OWNER_ID`
   - `LLM_KEY`
5. Open `@omniagentZbot` → send `/start`

## Commands

| Command    | Purpose                                |
|------------|----------------------------------------|
| `/start`   | wake bot                               |
| `/think`   | give instruction (plain text also works) |
| `/reload`  | restart after self-modification        |
| `/src`     | download current `bot.py`              |
| `/rollback`| restore previous backup                |
| `/health`  | status                                 |

## First self-modification
