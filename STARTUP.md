# Ouroboros — Startup Guide

Ouroboros is a self-hosted Telegram-driven AI agent. It runs on Google Colab (GPU-free) or any Linux machine with Python 3.11+. The supervisor communicates with you via Telegram; the agent reads and edits its own GitHub repo.

---

## Quick Start (Google Colab)

1. **Clone this repo** into your Colab session:
   ```python
   import subprocess, pathlib
   REPO = "/content/ouroboros_repo"
   GITHUB_USER = "your-user"
   GITHUB_REPO = "your-repo"
   GITHUB_TOKEN = "ghp_..."          # set as Colab Secret
   remote = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
   subprocess.run(["git", "clone", "--depth=1", "-b", "ouroboros", remote, REPO], check=True)
   import sys; sys.path.insert(0, REPO)
   ```

2. **Mount Google Drive** (the agent stores all persistent state here):
   ```python
   from google.colab import drive
   drive.mount("/content/drive")
   ```

3. **Set environment variables** (see [Required Secrets](#required-secrets) below), then run the supervisor:
   ```python
   exec(open(f"{REPO}/supervisor/main.py").read())
   ```

---

## Quick Start (Local with .env)

```bash
git clone -b ouroboros git@github.com:<you>/<repo>.git ouroboros_repo
cd ouroboros_repo
uv sync

cp .env.example .env
# Edit .env with your values — VLLM_BASE_URL, OUROBOROS_MODEL, TELEGRAM_BOT_TOKEN, etc.
$EDITOR .env

python -m supervisor.main
```

The `.env` file is loaded automatically at startup. It is already excluded from git via `.gitignore`.

---

## Running Locally (Manual env vars)

```bash
git clone -b ouroboros git@github.com:<you>/<repo>.git ouroboros_repo
cd ouroboros_repo
uv sync

export VLLM_BASE_URL=http://localhost:8000/v1
export VLLM_API_KEY=token                    # optional, defaults to 'token'
export TELEGRAM_BOT_TOKEN=123456:...
export GITHUB_TOKEN=ghp_...
export GITHUB_USER=your-user
export GITHUB_REPO=your-repo
export TOTAL_BUDGET=10.0
export OUROBOROS_MODEL=Qwen/Qwen2.5-72B-Instruct
export OUROBOROS_DRIVE_ROOT=/path/to/ouroboros-drive  # replaces Google Drive

python -m supervisor.main
```

---

## Required Secrets

| Variable | Required | Description |
|---|---|---|
| `VLLM_BASE_URL` | ✅ | vLLM endpoint, e.g. `http://localhost:8000/v1` |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `GITHUB_TOKEN` | ✅ | Personal access token with `repo` scope |
| `GITHUB_USER` | ✅ | GitHub username (owner of the repo) |
| `GITHUB_REPO` | ✅ | GitHub repo name |
| `TOTAL_BUDGET` | ✅ | Token budget limit (informational, no USD cost for local vLLM) |
| `VLLM_API_KEY` | ○ | API key if vLLM requires auth (default: `"token"`) |
| `OPENAI_API_KEY` | ○ | Optional (only needed if routing through OpenAI API) |

---

## Optional Configuration

| Variable | Default | Description |
|---|---|---|
| `OUROBOROS_MODEL` | — | Primary model as registered in vLLM (e.g. `Qwen/Qwen2.5-72B-Instruct`) |
| `OUROBOROS_MODEL_LIGHT` | — | Smaller/faster model on the same vLLM server (optional) |
| `OUROBOROS_MAX_WORKERS` | `5` | Max concurrent background tasks |
| `OUROBOROS_SOFT_TIMEOUT_SEC` | `600` | Soft timeout per task (sends warning) |
| `OUROBOROS_HARD_TIMEOUT_SEC` | `1800` | Hard timeout per task (kills task) |
| `OUROBOROS_MAX_ROUNDS` | `60` | Max LLM rounds per task |
| `OUROBOROS_PRE_PUSH_TESTS` | `1` | Run pytest before git push (`0` to disable) |
| `OUROBOROS_DIAG_HEARTBEAT_SEC` | `30` | Heartbeat log interval |
| `OUROBOROS_DRIVE_ROOT` | `/content/drive/MyDrive/Ouroboros` | Drive root for persistent state |

---

## Drive Layout

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
├── task_results/           # Completed subtask outputs
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
