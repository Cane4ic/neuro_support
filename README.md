# Neuro Uploader Support Bot

Telegram support bot with agent routing:
- User sends message to bot
- All agents receive ticket with `Accept/Reject` buttons
- First accepted agent is connected to user chat
- Agent and user can exchange text + media (photo/document/voice/etc.)
- Agent can close dialog with `/finish`

## Stack

- Python 3.11+
- `python-telegram-bot`
- Postgres (Supabase)
- Deployment: Railway

## Environment Variables

Set these in Railway service variables:

- `BOT_TOKEN` - Telegram bot token from BotFather
- `AGENT_IDS` - comma-separated Telegram user IDs of support agents, example:
  - `123456789,987654321`
- `DATABASE_URL` - Supabase Postgres connection string
  - Use the direct Postgres connection URL from Supabase project settings
  - Add `?sslmode=require` if needed

## Local Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="..."
export AGENT_IDS="123456789,987654321"
export DATABASE_URL="postgresql://postgres:password@host:5432/postgres?sslmode=require"
python neuro_support.py
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:BOT_TOKEN="..."
$env:AGENT_IDS="123456789,987654321"
$env:DATABASE_URL="postgresql://postgres:password@host:5432/postgres?sslmode=require"
python neuro_support.py
```

## Deploy to Railway with Supabase

1. Push this project to GitHub.
2. In Railway: `New Project` -> `Deploy from GitHub repo`.
3. Railway will install dependencies from `requirements.txt`.
4. In Railway Variables add:
   - `BOT_TOKEN`
   - `AGENT_IDS`
   - `DATABASE_URL` (from Supabase)
5. Start command:
   - `python neuro_support.py`
6. Deploy.

## Telegram Commands (Agent)

- `/start` - show info
- `/my` - show active ticket assigned to you
- `/finish` - close your active ticket
