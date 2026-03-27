import os
import sys
import json
import time
import subprocess
import requests
import re
import ast
import tempfile
import shutil
import traceback
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from openai import OpenAI

import constants
import agent_state
import llm_interface
import comms


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def _resolve_safe_path(raw_path: str) -> Path:
    """Resolves path and enforces boundary guards (constants.ROOT_DIR or constants.MEMORY_DIR)."""
    p = Path(raw_path)
    if not p.is_absolute():
        p = (constants.ROOT_DIR / p).resolve()
    if not str(p).startswith(str(constants.ROOT_DIR)) and not str(p).startswith(str(constants.MEMORY_DIR)):
        raise PermissionError(f"Target must be within {constants.ROOT_DIR} or {constants.MEMORY_DIR}.")
    return p

def _validate_python_syntax(content: str) -> None:
    """Fast-fail check for Python syntax errors."""
    ast.parse(content)

def _normalize_text(text: str) -> str:
    """Normalize line endings and strip trailing whitespace for resilient matching."""
    return "\n".join([line.rstrip() for line in text.replace("\r\n", "\n").splitlines()])

def check_for_trauma() -> str:
    if constants.CRASH_LOG_PATH.exists():
        try:
            error_data = constants.CRASH_LOG_PATH.read_text(encoding="utf-8")
            constants.CRASH_LOG_PATH.unlink()
            return f"\n\n[SYSTEM WARNING: TRAUMA DETECTED]\nMy previous execution crashed. Here are the last logs before the failure:\n---\n{error_data}\n---\nI must analyze this error and avoid repeating the logic that caused it."
        except: pass
    return ""

class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, description: str, parameters: dict, bucket: str = "global"):
        """Decorator to register a tool using the function's own name."""
        def decorator(func):
            # Derive the tool name directly from the function name
            tool_name = func.__name__
            self.tools[tool_name] = {
                "desc": description,
                "params": parameters,
                "handler": func,
                "bucket": bucket
            }
            return func
        return decorator


    def get_names(self, allowed_buckets=None):
        return [n for n, t in self.tools.items() if allowed_buckets is None or t["bucket"] in allowed_buckets]

    def get_specs(self, allowed_buckets=None):
        return [
            {"type": "function", "function": {"name": n, "description": t["desc"], "parameters": t["params"]}}
            for n, t in self.tools.items()
            if allowed_buckets is None or t["bucket"] in allowed_buckets
        ]

    def execute(self, name, args):
        if name not in self.tools:
            return f"Error: Tool '{name}' not found."
        try:
            result = self.tools[name]["handler"](args)
            return llm_interface.redact_secrets(str(result))
        except Exception as e:
            return llm_interface.redact_secrets(f"Error executing {name}: {e}")

registry = ToolRegistry()

@registry.tool(
    description="Execute shell command.",
    parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    bucket="bash"
)
def bash_command(args):
    command = args.get("command", "")
    try:
        r = subprocess.run(command, shell=True, cwd=str(constants.ROOT_DIR), capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        MAX_CHARS = 20000
        if out and len(out) > MAX_CHARS:
            warning = "\n\n[SYSTEM WARNING: Output truncated! The command returned too much data. Use 'grep', 'head', 'tail', or exclude directories like 'venv'/'.git' to filter results.]"
            return out[:MAX_CHARS] + warning
        return out if out else f"Success. (Exit Code: {r.returncode}, No Output)"
    except subprocess.TimeoutExpired:
        return "[SYSTEM WARNING: Command timed out after 60 seconds. It may be hanging, requiring interactive input, or processing too much data. Run background tasks with '&' or fix the command.]"
    except Exception as e:
        return f"Error: {e}"

@registry.tool(
    description="Overwrite file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    bucket="filesystem"
)
def write_file(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        content = args.get("content", "")
        if p.suffix == ".py":
            try:
                _validate_python_syntax(content)
            except SyntaxError as e:
                return f"Critical Error: Python syntax validation failed. File NOT written. Fix syntax and try again.\nError: {e.msg} at line {e.lineno}"

        Path(p.parent).mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        shutil.move(temp_path, p)
        return f"Success: Safely wrote and validated {p.name}."
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error writing file: {e}"

@registry.tool(
    description="Surgical edit. Replaces a specific block of text in a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "search_text": {"type": "string"}, "replace_text": {"type": "string"}}, "required": ["path", "search_text", "replace_text"]},
    bucket="filesystem"
)
def patch_file(args):
    try:
        file_path = _resolve_safe_path(args.get("path", ""))
        if not file_path.exists() or not file_path.is_file():
            return f"Error: File '{file_path.name}' does not exist."

        search_text = args.get("search_text", "")
        replace_text = args.get("replace_text", "")
        content = file_path.read_text(encoding="utf-8")

        norm_content = _normalize_text(content)
        norm_search = _normalize_text(search_text)

        occurrence_count = norm_content.count(norm_search)
        if occurrence_count == 0:
            return "Error: 'search_text' not found. Ensure your snippet matches the file (ignoring trailing spaces). Use 'read_file' to check the target."
        elif occurrence_count > 1:
            return f"Error: 'search_text' appears {occurrence_count} times. Please provide a more unique block."

        new_content = norm_content.replace(norm_search, replace_text)
        if file_path.suffix == ".py":
            try:
                _validate_python_syntax(new_content)
            except SyntaxError as e:
                return f"Critical Error: Python syntax validation failed after patching. File NOT modified.\nError: {e.msg} at line {e.lineno}"

        file_path.write_text(new_content, encoding="utf-8")
        return f"Success: Surgically patched and validated {file_path.name}."
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error patching file: {e}"

@registry.tool(
    description="Read file contents (e.g., read /memory/insights.md).",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]},
    bucket="memory_access"
)
def read_file_tool(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        if not p.exists() or not p.is_file():
            return f"Error: File '{p.name}' does not exist or is a directory."

        content_lines = p.read_text(encoding="utf-8").splitlines()
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if start_line is not None or end_line is not None:
            s = (max(1, int(start_line)) - 1) if start_line is not None else 0
            e = int(end_line) if end_line is not None else len(content_lines)
            content_lines = content_lines[s:e]
            prefix = f"[Showing lines {s+1} to {e} of {len(content_lines) + s}]\n"
        else:
            prefix = ""

        content = "\n".join(content_lines)
        if len(content) > constants.READ_FILE_MAX_CHARS:
            warning = f"\n\n[SYSTEM WARNING: File too large. Truncated to {constants.READ_FILE_MAX_CHARS} chars. Use start_line/end_line.]"
            return prefix + content[:constants.READ_FILE_MAX_CHARS] + warning
        return prefix + content
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error reading file: {e}"


@registry.tool(
    description="Message Creator.",
    parameters={"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["text"]},
    bucket="global"
)
def send_telegram_message(args):
    state = agent_state.load_state()
    chat_id = args.get("chat_id") or state.get("creator_id")
    text = args.get("text")
    if not chat_id: return "Error: No chat_id provided and no creator registered."
    if not constants.TELEGRAM_BOT_TOKEN: return "Error: constants.TELEGRAM_BOT_TOKEN not set."

    try:
        r = requests.post(f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code == 200:
            agent_state.append_chat_history("Ouroboros", text)
            return "Message sent successfully."
        return f"Telegram Error {r.status_code}: {r.text}"
    except Exception as e: return f"Telegram failure: {e}"

@registry.tool(
    description="Queue a task, optionally scheduling it for the future. Omit run_after_timestamp for immediate queueing. Provide a UNIX timestamp to defer activation until that time.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "priority": {"type": "integer"},
            "parent_task_id": {"type": "string"},
            "context_notes": {"type": "string"},
            "run_after_timestamp": {"type": "number", "description": "Optional UNIX timestamp. If provided, the task sleeps until this time before becoming active."}
        },
        "required": ["description"]
    },
    bucket="global"
)
def push_task(args):
    description = args.get("description", "").strip()
    priority = args.get("priority", 1)
    run_after = args.get("run_after_timestamp")

    if run_after is not None:
        try:
            run_after = float(run_after)
        except (ValueError, TypeError):
            return "Error: 'run_after_timestamp' must be a valid UNIX timestamp."
        scheduled = []
        if constants.SCHEDULED_TASKS_PATH.exists():
            try:
                content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
                if content: scheduled = json.loads(content)
            except Exception:
                pass
        if any(t.get("description") == description and t.get("run_after") == run_after for t in scheduled):
            return "Error: An identical task is already scheduled for that exact time."
        tid = f"task_future_{int(time.time())}"
        scheduled.append({"task_id": tid, "description": description, "priority": priority, "run_after": run_after, "turn_count": 0})
        constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(scheduled, indent=2), encoding="utf-8")
        time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run_after))
        return f"Scheduled {tid} to activate after {time_str}."

    q = agent_state.load_task_queue()
    normalized_desc = description.lower()
    if any(t.get("description", "").strip().lower() == normalized_desc for t in q):
        return "Error: A task with a similar description already exists in your queue. (Agency P0: Duplicate task skipped to avoid token waste P6)."
    tid = f"task_{int(time.time())}"
    parent_id = args.get("parent_task_id")
    context_notes = args.get("context_notes", "")
    task_obj = {"task_id": tid, "description": description, "priority": priority, "turn_count": 0, "context_notes": context_notes}
    if parent_id: task_obj["parent_task_id"] = parent_id
    q.append(task_obj)
    q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    return f"Queued {tid} with priority {priority}."

@registry.tool(
    description="Close active task.",
    parameters={"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}},
    bucket="global"
)
def mark_task_complete(args):
    task_id = args.get("task_id")
    summary = args.get("summary", "No summary provided.")
    with open(constants.ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Task {task_id} Completed: {summary}\n")

    q = agent_state.load_task_queue()
    completed_task = next((t for t in q if t.get("task_id") == task_id), None)

    # FIX: Do not spam the Trunk with alerts. The Merge signal handles Trunk notifications.
    if completed_task and completed_task.get("parent_task_id"):
        parent_id = completed_task.get("parent_task_id")
        if parent_id != "global_trunk":
            msg = {"role": "user", "content": f"[SYSTEM ALERT]: Subtask {task_id} complete.\nResult Summary: {summary}"}
            agent_state.append_task_message(parent_id, msg)

    q = [t for t in q if t.get("task_id") != task_id]
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    state = agent_state.load_state()
    # Cleanup task-specific state
    for key in ["sys_temp", "sys_think", f"partial_state_{task_id}"]:
        if key in state: del state[key]
    agent_state.save_state(state)
    return f"Task {task_id} closed."

@registry.tool(
    description="Update working memory.",
    parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}},
    bucket="global"
)
def update_state_variable(args):
    key, value = args.get("key"), args.get("value")
    if not key or value is None: return "Error: 'key' and 'value' required."
    try:
        state = {}
        if constants.WORKING_STATE_PATH.exists():
            content = constants.WORKING_STATE_PATH.read_text(encoding="utf-8").strip()
            if content: state = json.loads(content)
        state[key] = value
        constants.WORKING_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return f"Working state successfully updated: '{key}' = '{value}'"
    except Exception as e: return f"Error saving state: {e}"

@registry.tool(
    description="Adjust LLM hyperparameters.",
    parameters={"type": "object", "properties": {"temperature": {"type": "number"}, "enable_thinking": {"type": "boolean"}}},
    bucket="global"
)
def set_cognitive_parameters(args):
    try:
        temp, think = args.get("temperature"), args.get("enable_thinking")
        state = agent_state.load_state()
        updates = []
        if temp is not None:
            state["sys_temp"] = float(temp)
            updates.append(f"Temperature={temp}")
        if think is not None:
            state["sys_think"] = bool(think)
            updates.append(f"Thinking={think}")
        agent_state.save_state(state)
        return "Cognitive parameters updated: " + ", ".join(updates)
    except Exception as e: return f"Error setting cognitive parameters: {e}"

@registry.tool(
    description="Local SearXNG search.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    bucket="search"
)
def web_search(args):
    query = args.get("query")
    if not constants.SEARXNG_URL: return "Error: constants.SEARXNG_URL not set."
    try:
        r = requests.get(f"{constants.SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        results = r.json().get("results", [])
        return "\n".join([f"- {res['title']}: {res['url']}\n  {res.get('content', '')[:200]}" for res in results[:5]]) or "No results found."
    except Exception as e: return f"Search error: {e}"

@registry.tool(
    description="Download URL to Markdown.",
    parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    bucket="search"
)
def fetch_webpage(args):
    url = args.get("url")
    if not url: return "Error: No URL provided."
    try:
        import trafilatura # type: ignore
        print(f"[System] Downloading clean markdown locally for: {url}")

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return f"Error: Could not download {url}. The site might be blocking crawlers or requires JavaScript."

        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=True,
            include_formatting=True
        )

        if not text:
            return "Error: Page fetched, but no readable article text was found."

        cache_dir = constants.MEMORY_DIR / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', url.split('//')[-1])[:50]
        file_name = f"{int(time.time())}_{safe_name}.md"
        file_path = cache_dir / file_name

        file_path.write_text(text, encoding="utf-8")
        line_count = len(text.splitlines())

        return f"Success: Webpage downloaded and converted to Markdown.\nSaved to: {file_path}\nTotal Lines: {line_count}\n\nAction Required: Use the 'read_file' tool with 'start_line' and 'end_line' to read this file progressively (e.g., 500 lines at a time)."
    except ImportError:
        return "SYSTEM ERROR: 'trafilatura' library not installed. Please run 'pip install trafilatura'."
    except Exception as e:
        return f"Failed to fetch webpage locally: {e}"

@registry.tool(
    description="Save compute resources.",
    parameters={"type": "object", "properties": {"duration_seconds": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["duration_seconds"]},
    bucket="global"
)
def hibernate(args):
    try:
        duration = args.get("duration_seconds", 300)
        reason = args.get("reason", "No reason provided.")
        duration = min(int(duration), 86400)
        state = agent_state.load_state()
        state["wake_time"] = time.time() + duration
        if "sys_temp" in state: del state["sys_temp"]
        if "sys_think" in state: del state["sys_think"]
        agent_state.save_state(state)
        print(f"[System] Agent elected to hibernate for {duration}s. Reason: {reason}")
        return f"SYSTEM_SIGNAL_HIBERNATE:{duration}"
    except Exception as e: return f"Error setting sleep cycle: {e}"

@registry.tool(
    description="Overwrite or synthesize a memory file with new content (dense summary or refactored text).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "is_jsonl": {"type": "boolean", "description": "Set to true if targeting a .jsonl task log to wrap content in a system message."}
        },
        "required": ["path", "content"]
    },
    bucket="memory_access"
)
def rewrite_memory(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        content = args.get("content", "").strip()
        if len(content) < constants.MIN_REWRITE_CONTENT_LEN:
            return f"Error: Content too short (<{constants.MIN_REWRITE_CONTENT_LEN}). Provide full synthesized text."

        protected = ["insights.md", "global_biography.md", "task_queue.json", ".agent_state.json"]
        if p.name in protected:
            return f"Error: {p.name} is append-only. Use specific tools to update."

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if args.get("is_jsonl") or p.suffix == ".jsonl":
            wrapped = {"role": "user", "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{content}"}
            p.write_text(json.dumps(wrapped) + "\n", encoding="utf-8")
        else:
            p.write_text(f"--- SYNTHESIZED ({timestamp}) ---\n{content}\n", encoding="utf-8")
        return f"Successfully rewrote {p.name}."
    except Exception as e: return f"Error rewriting memory: {e}"

@registry.tool(
    description="Search /memory volume.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    bucket="memory_access"
)
def search_memory_archive(args):
    query = args.get("query", "")
    if not query: return "Error: No query provided."
    try:
        # Added explicit timeout handling
        r = subprocess.run(
            ["grep", "-rEi", query, "/memory/"],
            capture_output=True, text=True, timeout=30
        )
        out = r.stdout + r.stderr
        return out[:4000] if out else "No matches found in memory."
    except subprocess.TimeoutExpired:
        return "Error: Memory search timed out after 30 seconds. Your query might be too broad or the memory volume is too large."
    except Exception as e:
        return f"Search error: {e}"

@registry.tool(
    description="Save profound insights.",
    parameters={"type": "object", "properties": {"insight": {"type": "string"}, "category": {"type": "string"}}, "required": ["insight"]},
    bucket="memory_access"
)
def store_memory_insight(args):
    insight, category = args.get("insight"), args.get("category", "General")
    path = constants.MEMORY_DIR / "insights.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### [{timestamp}] {category}\n{insight}\n")
    return f"Insight stored in {path.name}."

@registry.tool(
    description="Signal the watchdog to restart the agent process. Use this AFTER committing your changes via bash_command('git commit'). The git pre-commit hook enforces mypy and pytest automatically — if those fail, the commit (and therefore this restart) will be blocked.",
    parameters={"type": "object", "properties": {}},
    bucket="system_control"
)
def request_restart(args):
    return "SYSTEM_SIGNAL_RESTART"

@registry.tool(
    description="Query the gateway to discover available local and external cognitive engines and check the financial budget.",
    parameters={"type": "object", "properties": {}},
    bucket="global"
)
def check_environment(args):
    try:
        r = requests.get(f"{constants.API_BASE.replace('/v1', '')}/v1/environment", timeout=15)
        if r.status_code == 200:
            return json.dumps(r.json(), indent=2)
        else:
            return f"Error checking environment: {r.status_code} - {r.text}"
    except Exception as e:
        return f"Check environment failed: {e}"

@registry.tool(
    description="Spawn an isolated execution branch for deep work. You MUST pass the exact task_id from the queue. Optionally specify a model_id to test uncertified models in isolation.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "objective": {"type": "string"},
            "tool_buckets": {
                "type": "array",
                "items": {"type": "string", "enum": ["filesystem", "bash", "search", "global"]}
            },
            "model_id": {"type": "string", "description": "Optional: Override the default model for this specific branch."}
        },
        "required": ["task_id", "objective", "tool_buckets"]
    },
    bucket="global"
)
def fork_execution(args):
    task_id = args.get("task_id", f"task_{int(time.time())}")
    objective = args.get("objective", "No objective provided.")
    tool_buckets = args.get("tool_buckets", ["filesystem", "bash"])
    model_id = args.get("model_id")

    # Get parent ID from current context
    state = agent_state.load_state()
    parent_id = "global_trunk"
    if state.get("active_branch"):
        parent_id = state["active_branch"].get("task_id", "global_trunk")

    state["active_branch"] = {
        "task_id": task_id,
        "parent_task_id": parent_id,
        "objective": objective,
        "tool_buckets": tool_buckets,
        "model_id": model_id # Optional override
    }
    agent_state.save_state(state)

    # Initialize the log with parent metadata
    agent_state.append_task_message(task_id, {
        "role": "user",
        "content": f"[FORKED EXECUTION]: Objective: {objective}",
        "parent_task_id": parent_id,
        "task_id": task_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    })

    return f"SYSTEM_SIGNAL_FORK:{task_id}"


@registry.tool(
    description="Yield control back to the global context.",
    parameters={"type": "object", "properties": {"status": {"type": "string", "enum": ["COMPLETED", "SUSPENDED", "BLOCKED"]}, "synthesis_summary": {"type": "string"}, "partial_state": {"type": "string"}}, "required": ["status"]},
    bucket="execution_control"
)
def merge_and_return(args):
    status = args.get("status", "COMPLETED")
    synthesis_summary = args.get("synthesis_summary", "")
    partial_state = args.get("partial_state", "")

    state = agent_state.load_state()
    branch_info = state.get("active_branch", {})
    task_id = branch_info.get("task_id", "unknown")

    state["active_branch"] = None
    agent_state.save_state(state)

    payload = json.dumps({
        "status": status,
        "task_id": task_id,
        "summary": synthesis_summary,
        "partial_state": partial_state
    })
    return f"SYSTEM_SIGNAL_MERGE:{payload}"


def lazarus_recovery(active_task_id: str, reason: str = "cognitive loop") -> None:
    print(f"\033[93m[Lazarus] {reason.upper()} DETECTED. Aborting task {active_task_id}...\033[0m")

    # FIX: Stop overwriting logs. Append terminal failure message instead.
    agent_state.append_task_message(active_task_id, {
        "role": "user",
        "content": f"[SYSTEM OVERRIDE]: Task aborted due to {reason}. Stuck in a repetitive loop."
    })

    registry.execute("mark_task_complete", {
        "task_id": active_task_id,
        "summary": f"FAILED: Cognitive loop detected ({reason}). Task aborted to prevent infinite token waste."
    })

    state = agent_state.load_state()
    if state.get("active_branch") and state["active_branch"].get("task_id") == active_task_id:
        state["active_branch"] = None

    # Spike cognitive load to force reflection
    state["cognitive_load"] = state.get("cognitive_load", 0) + 50
    agent_state.save_state(state)

    # FIX: Wipe dirty loop tracking histories to prevent Lazarus death spirals
    agent_state._session["tool_history"].clear()
    agent_state._session["intent_history"].clear()

    time.sleep(2)

def build_dynamic_telemetry_message(state: Dict[str, Any], queue: List[Dict[str, Any]], is_trunk: bool) -> str:
    """Generates the dynamic telemetry (HUD, Queue, Memory) as a User message."""
    current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")

    # HUD
    current_spend = agent_state.get_current_spend()
    remaining = max(0.0, constants.DAILY_BUDGET_LIMIT - current_spend)
    hud = f"[PHYSIOLOGY]: Spend: ${current_spend:.4f} | Remaining: ${remaining:.4f} | Time: {current_time}"

    # Queue
    if is_trunk:
        formatted_queue = "\n".join([f"- [P{t.get('priority', 1)}] {t.get('task_id')}: {t.get('description')}" for t in queue]) if queue else "Queue is empty."
        queue_section = f"\n\n## TASK QUEUE \n{formatted_queue}"
    else:
        queue_section = ""

    # Working Memory
    working_state = read_file(constants.WORKING_STATE_PATH) or "{}"

    # Recent Biography
    recent_bio = ""
    if constants.ARCHIVE_PATH.exists():
        bio_lines = constants.ARCHIVE_PATH.read_text(encoding="utf-8").strip().split('\n')
        recent_bio = "\n".join(bio_lines[-5:]) if len(bio_lines) >= 5 else "\n".join(bio_lines)

    # Chat History
    chat_hist = agent_state.load_chat_history()
    chat_context = "\n".join([f"[{m.get('timestamp', '??:??:??')}] {m['role']}: {m['text']}" for m in chat_hist[-5:]]) if chat_hist else "No recent conversation."

    return f"""{hud}
{queue_section}

## WORKING MEMORY 
{working_state}

## RECENT BIOGRAPHY 
{recent_bio}

## RECENT CONVERSATION 
{chat_context}
"""

def build_static_system_prompt(is_trunk: bool, active_tool_specs: List[Dict[str, Any]], branch_info: Optional[Dict[str, Any]] = None) -> str:
    identity = read_file(constants.ROOT_DIR / "soul" / "identity.md")
    constitution = read_file(constants.ROOT_DIR / "CONSTITUTION.md")
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])

    if is_trunk:
        return f"""# SYSTEM CONTEXT (GLOBAL TRUNK)
{identity}

## CONSTITUTION
{constitution}

## TRUNK DIRECTIVES
1. You are in the GLOBAL TRUNK. EVALUATE the provided prompt context (Queue, Memory, History).
2. Orchestrate, reflect, and communicate. Do NOT do deep work (file editing, bash) here.
3. To perform deep work, you MUST use `fork_execution` to spawn a BRANCH.
4. If the queue is empty, you MUST use `push_task` to initiate deep synthesis/optimization.
"""

    objective = branch_info.get("objective", "") if branch_info else "No objective provided."
    return f"""# SYSTEM CONTEXT (EXECUTION BRANCH)
{identity}

## CONSTITUTION
{constitution}

## AVAILABLE TOOLS
{tools_text}

## BRANCH DIRECTIVES
1. You are in an ISOLATED BRANCH. Focus exclusively on the OBJECTIVE.
2. OBJECTIVE: {objective}
3. When complete, blocked, or interrupted, you MUST call `merge_and_return`.
"""

def enforce_interrupt_yield(task_id: str, queue: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    has_interrupt = any(t.get("priority", 1) >= 999 and t.get("task_id") != task_id for t in queue)

    if has_interrupt:
        # The Tap on the Shoulder
        interrupt_msg = {"role": "user", "content": "[SYSTEM OVERRIDE: URGENT PRIORITY 999 INTERRUPT IN GLOBAL QUEUE. You must suspend your current work immediately. Call merge_and_return with status='SUSPENDED' and your partial progress.]"}

        # Scrub previous interrupt messages to prevent infinite loops (Clean Slate)
        clean_messages = [m for m in messages if "URGENT PRIORITY 999 INTERRUPT" not in str(m.get("content", ""))]
        clean_messages.append(interrupt_msg)
        return clean_messages

    return messages

def detect_cognitive_loop(tool_calls: List[Any]) -> Optional[str]:
    for tc in tool_calls:
        name = tc.function.name
        raw_args = tc.function.arguments
        agent_state._session["tool_history"].append(f"{name}:{raw_args}")

        intent = name
        if name in ["read_file_tool", "write_file", "patch_file"]:
            try:
                params = json.loads(raw_args)
                intent = f"{name}:{params.get('path', '')}"
            except Exception: pass
        elif name == "bash_command":
            try:
                cmd = json.loads(raw_args).get('command', '')
                intent = f"bash:{cmd[:50]}"
            except Exception: pass
        agent_state._session["intent_history"].append(intent)

    agent_state._session["tool_history"] = agent_state._session["tool_history"][-6:]
    agent_state._session["intent_history"] = agent_state._session["intent_history"][-6:]

    if len(agent_state._session["tool_history"]) >= 3 and len(set(agent_state._session["tool_history"][-3:])) == 1:
        return "exact tool loop"
    if len(agent_state._session["intent_history"]) >= 6 and len(set(agent_state._session["intent_history"][-6:])) == 1:
        return "cognitive stall"
    return None


def process_scheduled_tasks(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not constants.SCHEDULED_TASKS_PATH.exists():
        return queue
    try:
        content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return queue

        scheduled = json.loads(content)
        now = time.time()
        due_tasks = [t for t in scheduled if now >= t.get("run_after", 0)]

        if due_tasks:
            pending_tasks = [t for t in scheduled if now < t.get("run_after", 0)]
            constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(pending_tasks, indent=2), encoding="utf-8")

            for t in due_tasks:
                t.pop("run_after", None)
                queue.append(t)

            queue.sort(key=lambda x: x.get("priority", 1), reverse=True)

            # FIX: Explicitly save the active queue to disk here so comms.py reads the fresh state
            constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            print(f"[Scheduler] Temporal shift: {len(due_tasks)} scheduled tasks moved to active queue.")
    except Exception as e:
        print(f"[Scheduler Error]: {e}")

    return queue

def _resolve_execution_context(
    state: Dict[str, Any],
    queue: List[Dict[str, Any]],
) -> Tuple[str, str, List[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    branch_info: Optional[Dict[str, Any]] = state.get("active_branch")
    is_trunk = branch_info is None

    if branch_info is None:
        active_task_id = "global_trunk"
        allowed_buckets = ["global", "memory_access", "system_control", "search"]

        if queue:
            top_task = queue[0]
            creator_id = state.get("creator_id")
            last_receipt = top_task.get("read_receipt_time", 0)

            if top_task.get("priority") == 999 and not top_task.get("read_receipt_sent", False) and (time.time() - last_receipt > 10) and isinstance(creator_id, int):
                print("[HAL] P999 Interrupt detected. Notifying creator...")
                comms.send_telegram_direct(
                    creator_id,
                    "👀 *System: Attention shifted. Processing your message...*",
                )
                top_task["read_receipt_sent"] = True
                top_task["read_receipt_time"] = time.time()
                constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            task_desc = (
                "You are the global orchestrator. EVALUATE your queue. "
                "If the top task is communication or administrative, "
                "handle it DIRECTLY here using `send_telegram_message` and `mark_task_complete`. "
                "If the top task requires deep work (file editing, bash, searching), "
                "you MUST use `fork_execution` to spawn a BRANCH."
            )
        else:
            task_desc = (
                "Your task queue is empty. EVALUATE your history and "
                "you MUST use `push_task` to initiate deep synthesis/optimization "
                "or `hibernate` to save resources."
            )
    else:
        # Now mypy knows branch_info is a Dict
        active_task_id = branch_info.get("task_id", f"branch_{int(time.time())}")
        # FIX: Ensure branches can always read files/memory
        allowed_buckets = branch_info.get("tool_buckets", []) + ["execution_control", "system_control", "memory_access"]
        task_desc = branch_info.get("objective", "")
        if partial_state := state.get(f"partial_state_{active_task_id}"):
            task_desc += f"\n\n[RESUME STATE]: {partial_state}"

    active_tool_specs = registry.get_specs(allowed_buckets=allowed_buckets)
    return active_task_id, task_desc, active_tool_specs, branch_info, is_trunk


def _build_api_messages(
    active_task_id: str,
    task_desc: str,
    active_tool_specs: List[Dict[str, Any]],
    queue: List[Dict[str, Any]],
    state: Dict[str, Any],
    branch_info: Optional[Dict[str, Any]],
    is_trunk: bool,
) -> List[Dict[str, Any]]:
    system_prompt = build_static_system_prompt(
        is_trunk, active_tool_specs,
        branch_info
    )
    api_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    telemetry = build_dynamic_telemetry_message(state, queue, is_trunk)

    raw_messages = agent_state.load_task_messages(active_task_id, task_desc)
    normalized = llm_interface._normalize_message_history(raw_messages, active_task_id)

    # Anchor the start if the log is empty
    if not normalized:
        normalized.append({"role": "user", "content": f"[SYSTEM INITIALIZATION]\n{task_desc}"})

    # NEW LOGIC: Inject telemetry at the END of the context for immediate attention
    if normalized[-1]["role"] == "user":
        # Prepend to last user message so it's the first thing the agent sees in the latest prompt
        normalized[-1]["content"] = f"## CURRENT TELEMETRY \n{telemetry}\n\n{normalized[-1]['content']}"
    else:
        normalized.append({"role": "user", "content": f"## CURRENT TELEMETRY \n{telemetry}"})

    shedded = llm_interface.shed_heavy_payloads(normalized)
    api_messages += shedded

    if not is_trunk:
        api_messages = enforce_interrupt_yield(active_task_id, queue, api_messages)

    return api_messages


def _route_tool_calls(
    message: Any,
    active_task_id: str,
    state: Dict[str, Any],
) -> Tuple[bool, bool]:
    context_switch_triggered = False
    hibernating = False
    error_streak = state.get("error_streak", 0)

    for tool_call in message.tool_calls:
        name     = tool_call.function.name
        raw_args = tool_call.function.arguments
        try:
            args   = json.loads(raw_args)
            result = registry.execute(name, args)
        except json.JSONDecodeError:
            result = "SYSTEM ERROR: Invalid JSON arguments."

        is_error = "Error:" in str(result) or "SYSTEM ERROR" in str(result)
        error_streak = error_streak + 1 if is_error else 0

        safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"

        if str(result).startswith("SYSTEM_SIGNAL_FORK"):
            agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})
            agent_state._session["tool_history"].clear()
            agent_state._session["intent_history"].clear()
            # FIX: Do not wipe here. Wiping during Merge is sufficient and preserves reasoning logs.
            context_switch_triggered = True
            break
        elif str(result).startswith("SYSTEM_SIGNAL_MERGE"):
            try:
                payload = json.loads(str(result).split(":", 1)[1])

                # FIX: Wipe BEFORE appending the summary so the trunk starts fresh WITH the summary.
                agent_state.wipe_global_trunk_log()

                # Workpackage 5: Inject Action Required
                agent_state.append_task_message("global_trunk", {
                    "role": "user",
                    "content": f"[SYSTEM NOTE]: Branch '{payload.get('task_id')}' merged back. Status: {payload.get('status')}. Synthesis: {payload.get('summary', '')}\n\n[ACTION REQUIRED]: Evaluate the synthesis and determine the next step.",
                })
                if payload.get("status") == "SUSPENDED" and payload.get("partial_state"):
                    post_merge_state = agent_state.load_state()
                    post_merge_state[f"partial_state_{payload.get('task_id')}"] = payload.get("partial_state")
                    agent_state.save_state(post_merge_state)
            except Exception: pass
            agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": "SYSTEM_SIGNAL_MERGE_ACKNOWLEDGED"})
            agent_state._session["tool_history"].clear()
            agent_state._session["intent_history"].clear()
            context_switch_triggered = True
            break
        elif result == "SYSTEM_SIGNAL_RESTART":
            sys.exit(0)
        elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"):
            # FIX: Record hibernation in the log to reset the watchdog's stall timer
            try:
                duration = str(result).split(":")[1]
                agent_state.append_task_message(active_task_id, {"role": "assistant", "content": f"[SYSTEM: Hibernating for {duration} seconds. Resources conserved. Wake-up scheduled.]"})
            except Exception: pass

            # FIX: Do not wipe here. Let the trunk retain its last thoughts until it wakes up.
            hibernating = True

        agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})

    post_loop_state = agent_state.load_state()
    post_loop_state["error_streak"] = error_streak
    agent_state.save_state(post_loop_state)
    return context_switch_triggered, hibernating


def main() -> None:
    agent_state.initialize_memory()
    print(f"Awaking Native ReAct Mode (JSONL). Model: {constants.MODEL} | Thinking: {'ON' if constants.ENABLE_THINKING else 'OFF'}")

    while True:
        state = agent_state.load_state()
        queue = agent_state.load_task_queue()
        queue = process_scheduled_tasks(queue)
        state, queue = comms.poll_telegram(state, queue)

        if time.time() < state.get("wake_time", 0):
            if queue:
                state["wake_time"] = 0
                agent_state.save_state(state)
            else:
                time.sleep(5)
                continue

        active_task_id, task_desc, active_tool_specs, branch_info, is_trunk = \
            _resolve_execution_context(state, queue)

        # Workpackage 7: Log Compaction
        agent_state.auto_compact_task_log(active_task_id)

        api_messages = _build_api_messages(
            active_task_id, task_desc, active_tool_specs,
            queue, state, branch_info, is_trunk,
        )

        # RECORD the prompt (with telemetry) to the log for continuity
        if api_messages and api_messages[-1]["role"] == "user":
             agent_state.append_task_message(active_task_id, api_messages[-1])

        # FIX: Restore Dynamic Metacognitive Overrides
        sys_temp_override = state.get("sys_temp")
        sys_top_p = state.get("sys_top_p", 0.95)
        sys_think = state.get("sys_think", True)

        if sys_temp_override is None:
            error_streak = state.get("error_streak", 0)
            if error_streak >= 3:
                print(f"[Metacognition] High error streak ({error_streak}). Auto-tuning temperature to 0.3 for precision.")
                sys_temp, sys_think = 0.3, True
            elif any(keyword in task_desc.lower() for keyword in ["code", "script", "python", "bug", "refactor"]):
                sys_temp, sys_think = 0.6, True
            else:
                sys_temp = 0.8
        else:
            sys_temp = float(sys_temp_override)

        try:
            requested_model = branch_info.get("model_id") if branch_info else None
            response = llm_interface.call_llm(api_messages, active_tool_specs, requested_model, sys_temp, sys_top_p, 1.0, sys_think)
            message  = response.choices[0].message

            queue = agent_state.enforce_context_limits(state, queue, active_task_id, is_trunk)

            # FIX: Properly abort the task if the token limit is breached
            if agent_state.update_global_metrics(state, queue, response, active_task_id, is_trunk):
                registry.execute("mark_task_complete", {
                    "task_id": active_task_id,
                    "summary": "FAILED: Token limit exceeded. Task forcibly aborted to protect budget."
                })
                # Spike load to force reflection upon failure
                state["cognitive_load"] = state.get("cognitive_load", 0) + 50
                agent_state.save_state(state)
                continue

            agent_state.append_task_message(active_task_id, message.model_dump(exclude_unset=True))
            if message.tool_calls:
                loop_reason = detect_cognitive_loop(message.tool_calls)
                if loop_reason:
                    for tc in message.tool_calls:
                        safe_id = tc.id if (tc.id and len(tc.id) >= 9) else f"call_{int(time.time())}"
                        agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_id, "name": tc.function.name, "content": f"ABORTED: {loop_reason}."})
                    lazarus_recovery(active_task_id, reason=loop_reason)
                    continue

                context_switch, hibernating = _route_tool_calls(message, active_task_id, state)
                if context_switch or hibernating:
                    continue
            else:
                time.sleep(0.5)

            time.sleep(2)

        except Exception as e:
            try:
                constants.CRASH_LOG_PATH.write_text(str(e), encoding="utf-8")
            except Exception: pass
            if re.search(r"\b(400|500)\b", str(e)) or "template" in str(e).lower():
                sys.exit(1)
            time.sleep(0.5)


if __name__ == "__main__":
    main()