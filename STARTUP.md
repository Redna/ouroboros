# Ouroboros — Startup Guide

Ouroboros is a self-hosted Telegram-driven AI agent. It runs in a Linux Docker environment or any Linux machine with Python 3.11+. The supervisor communicates with you via Telegram; the agent reads and edits its own GitHub repo.

---

## Quick Start (Docker)

1. **Fork this repo** on GitHub.
2. **Clone your fork** and run with Docker Compose:
   ```bash
   git clone -b ouroboros git@github.com:<you>/ouroboros.git
   cd ouroboros
   docker-compose up -d
   ```

---

## Quick Start (Local with .env)

```bash
git clone -b ouroboros git@github.com:<you>/ouroboros.git
cd ouroboros
uv sync

cp .env.example .env
# Edit .env with your values — VLLM_BASE_URL, OUROBOROS_MODEL, TELEGRAM_BOT_TOKEN, etc.
$EDITOR .env

python -m supervisor.main
```

The `.env` file is loaded automatically at startup. It is already excluded from git via `.gitignore`.

---

## Required Secrets

| Variable | Required | Description |
|---|---|---|
| `VLLM_BASE_URL` | ✅ | vLLM endpoint, e.g. `http://localhost:8000/v1` |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `GITHUB_TOKEN` | ✅ | Personal access token with `repo` scope |
| `GITHUB_USER` | ✅ | GitHub username (owner of the repo) |
| `GITHUB_REPO` | ✅ | GitHub repo name |
| `OUROBOROS_DRIVE_ROOT`| ✅ | Path to the persistent volume for state and logs (e.g. `/drive`) |
| `VLLM_API_KEY` | ○ | API key if vLLM requires auth (default: `"token"`) |

---

## Optional Configuration

| Variable | Default | Description |
|---|---|---|
| `OUROBOROS_MODEL` | — | Primary model as registered in vLLM (e.g. `Qwen/Qwen2.5-72B-Instruct`) |
| `OUROBOROS_MODEL_LIGHT` | — | Smaller/faster model on the same vLLM server (optional) |
| `OUROBOROS_MAX_WORKERS` | `5` | Max concurrent background tasks |
| `OUROBOROS_SOFT_TIMEOUT_SEC` | `600` | Soft timeout per task (sends warning) |
| `OUROBOROS_HARD_TIMEOUT_SEC` | `1800` | Hard timeout per task (kills task) |
| `OUROBOROS_MAX_ROUNDS` | `200` | Max LLM rounds per task |
| `OUROBOROS_PRE_PUSH_TESTS` | `1` | Run pytest before git push (`0` to disable) |
| `OUROBOROS_DIAG_HEARTBEAT_SEC` | `30` | Heartbeat log interval |

---

## Storage Layout

The agent stores all persistent state in `OUROBOROS_DRIVE_ROOT`:

```
Ouroboros/
├── memory/
│   ├── scratchpad.md       # Agent working memory
│   ├── identity.md         # Agent identity manifest
│   └── knowledge/          # Topic-based knowledge base
├── logs/
│   ├── chat.jsonl          # Chat history
│   ├── events.jsonl        # Tool call events
│   └── supervisor.jsonl    # Supervisor heartbeats
├── state/
│   └── state.json          # Runtime state (budget, offsets, etc.)
└── locks/                  # Git lock files
```

---

## Telegram Commands

Once your bot is running and you've sent it the first message (which registers you as owner):

| Command | Description |
|---|---|
| `/status` | Show queue and worker status |
| `/review` | Queue a code review task |
| `/evolve on/off` | Toggle self-improvement mode |
| `/bg start/stop/status` | Control background consciousness |
| `/restart` | Soft-restart the supervisor |
| `/panic` | Emergency stop (kills all workers) |

---

## Branches

| Branch | Description |
|---|---|
| `ouroboros` | Development branch — where the agent makes edits |
| `ouroboros-stable` | Stable branch — promoted via `promote_to_stable` tool |
| `main` | Your project / base code |
