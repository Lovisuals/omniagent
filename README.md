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
## Cross-Domain Assistant Mode

OmniAgent is not just a dev bot. Text it any question:

- "What's a good recipe for 3-ingredient dinner?"
- "Summarize https://example.com/article"
- "Explain the difference between REST and GraphQL"
- "Current price of ETH"  ← triggers web search
- "Plan my week given these 5 tasks: ..."

It decides automatically whether to answer from knowledge, search the web, or fetch a URL.

## Optional: Repo-Privacy Self-Audit

To enable boot-time and `/health` privacy checking, add two more env vars on Railway:

| Key | Value |
|---|---|
| `GITHUB_REPO` | `Lovisuals/omniagent` |
| `GITHUB_TOKEN` | A GitHub Personal Access Token with `repo` read scope |

Create token at: github.com → Settings → Developer settings → Personal access tokens → Fine-grained → Select `omniagent` repo → Permissions: **Metadata: Read-only** → Generate.

If the repo ever goes public, `/health` will show `⚠️ REPO IS PUBLIC` and logs will shout.

## Adding New Capabilities

Just ask in chat. Examples:

- `/think add a tool that reads my Gmail inbox using an OAuth token I'll provide`
- `/think add a /remind command that schedules messages`
- `/think integrate OpenWeatherMap — I have a key ready`

OmniAgent will read its own source, write the new tool, and save a backup. Send `/reload` to activate.
