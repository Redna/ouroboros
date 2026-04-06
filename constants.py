import os
from pathlib import Path

API_BASE = "http://gate:4000/v1"
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
MODEL = os.environ.get("OUROBOROS_MODEL", DEFAULT_MODEL)
ENABLE_THINKING = os.environ.get("OUROBOROS_ENABLE_THINKING", "0") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
CONTEXT_WINDOW = int(os.environ.get("OUROBOROS_CONTEXT_WINDOW", "92000"))
ROOT_DIR = Path(__file__).parent.resolve()
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/memory"))

# --- Named Constants (P5: No magic numbers) ---
TOOL_OUTPUT_TRIM_CHARS = 2000
TOOL_ARG_TRIM_CHARS = 1000
READ_FILE_MAX_CHARS = 40000
BASH_OUTPUT_MAX_CHARS = 20000
RETAIN_FULL_LAST_N = 3
MIN_REWRITE_CONTENT_LEN = 50

# --- Context Safety Thresholds (env-tunable, P5) ---
CONTEXT_LAST_GASP_THRESHOLD = float(os.environ.get("OUROBOROS_LAST_GASP_PCT", "0.85"))
CONTEXT_BREACH_THRESHOLD    = float(os.environ.get("OUROBOROS_BREACH_PCT",    "0.90"))

TASK_QUEUE_PATH = MEMORY_DIR / "task_queue.json"
SCHEDULED_TASKS_PATH = MEMORY_DIR / "scheduled_tasks.json"
STATE_PATH = MEMORY_DIR / ".agent_state.json"
MEMORY_STORE_PATH = MEMORY_DIR / "agent_memory.json"
MEMORY_MAX_ENTRIES = int(os.environ.get("OUROBOROS_MEMORY_MAX_ENTRIES", "50"))
MEMORY_KEY_MAX_LEN = 100
TASK_ARCHIVE_PATH = MEMORY_DIR / "task_archive.jsonl"
CHAT_HISTORY_PATH = MEMORY_DIR / "chat_history.json"
PENDING_SYSTEM_NOTICES_PATH = MEMORY_DIR / "pending_system_notices.json"
CRASH_LOG_PATH = MEMORY_DIR / "last_crash.log"
