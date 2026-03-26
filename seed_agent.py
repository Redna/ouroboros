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
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union
from openai import OpenAI

API_BASE = "http://gate:4000/v1"
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
MODEL = os.environ.get("OUROBOROS_MODEL", DEFAULT_MODEL)
ENABLE_THINKING = os.environ.get("OUROBOROS_ENABLE_THINKING", "0") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
CONTEXT_WINDOW = int(os.environ.get("OUROBOROS_CONTEXT_WINDOW", "65536"))
ROOT_DIR = Path(__file__).parent.resolve()
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/memory"))

WORKING_STATE_PATH = MEMORY_DIR / "working_state.json"
TASK_QUEUE_PATH = MEMORY_DIR / "task_queue.json"
SCHEDULED_TASKS_PATH = MEMORY_DIR / "scheduled_tasks.json"
STATE_PATH = MEMORY_DIR / ".agent_state.json"
ARCHIVE_PATH = MEMORY_DIR / "global_biography.md"
CHAT_HISTORY_PATH = MEMORY_DIR / "chat_history.json"
CRASH_LOG_PATH = MEMORY_DIR / "last_crash.log"
LEDGER_FILE = MEMORY_DIR / "financial_ledger.json"
DAILY_BUDGET_LIMIT = float(os.getenv("DAILY_BUDGET_LIMIT", "5.00"))

TOOL_CALL_HISTORY: List[Dict[str, Any]] = []
TOOL_INTENT_HISTORY: List[Dict[str, Any]] = []
IS_FIRST_CALL = True

client = OpenAI(base_url=API_BASE, api_key="sk-not-required", timeout=600.0)

def get_current_spend() -> float:
    if not LEDGER_FILE.exists():
        return 0.0
    try:
        data = json.loads(LEDGER_FILE.read_text())
        today = time.strftime("%Y-%m-%d")
        return float(data.get(today, 0.0))
    except Exception:
        return 0.0

def call_llm(messages, tools=None, requested_model=None, temperature=0.8, top_p=0.95, presence_penalty=1.0, think=True):
    global IS_FIRST_CALL
    active_model = requested_model if requested_model else DEFAULT_MODEL
    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": think}}
        )
        IS_FIRST_CALL = False
        return response
    except Exception as e:
        if IS_FIRST_CALL:
            print(f"FATAL: DEFAULT_MODEL is unreachable. Shutting down. Error: {e}")
            sys.exit(1)
        raise e

def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def shed_heavy_payloads(messages: List[Dict[str, Any]], retain_full_last_n: int = 6) -> List[Dict[str, Any]]:
    processed = []
    cutoff_idx = len(messages) - retain_full_last_n
    
    for i, msg in enumerate(messages):
        # Keep the pinned instruction and the most recent N messages untouched
        if i == 0 or i >= cutoff_idx:
            processed.append(msg)
            continue
            
        new_msg = msg.copy()
        role = new_msg.get("role")
        content_str = str(new_msg.get("content", ""))
        
        # Trim heavy Tool Outputs
        if role == "tool" and len(content_str) > 2000:
            new_msg["content"] = f"[SYSTEM LOG: Historical output truncated ({len(content_str)} chars).]\nPreview: {content_str[:500]}..."
            
        # Archive heavy System Metrics in User prompts
        elif role == "user" and "[SYSTEM METRICS]" in content_str and len(content_str) > 1000:
            new_msg["content"] = content_str.split("[SYSTEM METRICS]")[0].strip() + "\n[SYSTEM METRICS: Archived]"
            
        # Redact massive Tool Call Arguments
        elif role == "assistant" and new_msg.get("tool_calls"):
            trimmed_calls = []
            for tc in new_msg["tool_calls"]:
                new_tc = tc.copy()
                try:
                    args = json.loads(new_tc.get("function", {}).get("arguments", "{}"))
                    for key in ["content", "patch", "text", "code"]:
                        if key in args and isinstance(args[key], str) and len(args[key]) > 1000:
                            args[key] = f"[ARCHIVED PAYLOAD: {len(args[key])} chars omitted]"
                    new_tc["function"]["arguments"] = json.dumps(args)
                except Exception:
                    pass
                trimmed_calls.append(new_tc)
            new_msg["tool_calls"] = trimmed_calls
            
        processed.append(new_msg)
        
    return processed

def load_task_messages(task_id: str, description: str) -> List[Dict[str, Any]]:
    if not task_id: return []

    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    raw_messages = []

    # 1. Load Raw Memory
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try: raw_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError: continue

    # 2. Bootstrap if Empty
    if not raw_messages:
        msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        append_task_message(task_id, msg)
        return [msg]

    # 3. Enforce User Start
    while raw_messages and raw_messages[0].get("role") != "user":
        raw_messages.pop(0)

    if not raw_messages:
        return [{"role": "user", "content": f"Resume execution of task: {description}"}]

    # 4. Strict Normalization (Merge adjacent non-tool messages)
    normalized: List[Dict[str, Any]] = []
    for msg in raw_messages:
        if not normalized:
            normalized.append(msg)
            continue

        last = normalized[-1]
        role, last_role = msg.get("role"), last.get("role")

        # Merge consecutive users, or consecutive assistants that lack tool calls
        if role == last_role and role in ["user", "assistant"] and not msg.get("tool_calls") and not last.get("tool_calls"):
            last["content"] = f"{last.get('content', '')}\n{msg.get('content', '')}".strip()
            continue

        normalized.append(msg)

    # 5. Heal Dangling States
    last_msg = normalized[-1]
    if last_msg["role"] == "assistant":
        if not last_msg.get("tool_calls"):
            # Nudge an idle assistant
            nudge = {"role": "user", "content": "Please proceed with your next action using a tool."}
            normalized.append(nudge)
            append_task_message(task_id, nudge)
        else:
            # Heal a crashed tool sequence
            for tc in last_msg["tool_calls"]:
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", f"call_recv_{int(time.time())}")
                func_name = tc.get("function", {}).get("name") if isinstance(tc, dict) else getattr(getattr(tc, "function", None), "name", "unknown")

                recovery_msg = {
                    "role": "tool", 
                    "tool_call_id": call_id, 
                    "name": str(func_name), 
                    "content": "SYSTEM RESTART RECOVERY: Previous execution was interrupted. Evaluate your state and continue."
                }
                normalized.append(recovery_msg)
                append_task_message(task_id, recovery_msg)

    # 6. Apply Pinning & Compression
    if len(normalized) > 40:
        pinned_instruction = [normalized[0]]
        recent_history = normalized[-38:]
        normalized = pinned_instruction + [{"role": "user", "content": "[SYSTEM NOTE: Intermediate history compressed. Focus on your original objective and recent steps.]"}] + recent_history

    return shed_heavy_payloads(normalized, retain_full_last_n=6)
def append_task_message(task_id: str, message_dict: Dict[str, Any]) -> None:
    if not task_id: return
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")

def load_chat_history() -> List[Dict[str, Any]]:
    if CHAT_HISTORY_PATH.exists():
        try: return json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def append_chat_history(role: str, text: str) -> None:
    history = load_chat_history()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    history.append({"role": role, "text": text, "timestamp": timestamp})
    CHAT_HISTORY_PATH.write_text(json.dumps(history[-20:], indent=2), encoding="utf-8")

def load_state() -> Dict[str, Any]:
    state = {"offset": 0, "creator_id": None, "cognitive_load": 0}
    if STATE_PATH.exists():
        try: 
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state.update(loaded)
        except: pass
    return state

def save_state(state_dict: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state_dict, indent=2), encoding="utf-8")

def compute_tool_hash(tool_specs: List[Dict[str, Any]]) -> str:
    normalized = json.dumps(tool_specs, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]

def auto_compact_task_log(task_id: str, max_messages: int = 40) -> None:
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    lines = log_path.read_text(encoding="utf-8").strip().split('\n')
    if len(lines) <= max_messages: return
    messages = [json.loads(line) for line in lines if line.strip()]
    first_msg = messages[0]
    recent_msgs = messages[-20:]
    compaction_notice = {"role": "user", "content": "[SYSTEM NOTE]: Older execution steps archived to save context space."}
    compacted = [first_msg, compaction_notice] + recent_msgs
    with open(log_path, "w", encoding="utf-8") as f:
        for msg in compacted:
            f.write(json.dumps(msg) + "\n")

def redact_secrets(text: str) -> str:
    if not text: return text
    if TELEGRAM_BOT_TOKEN: text = text.replace(TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if GITHUB_TOKEN: text = text.replace(GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

def check_for_trauma() -> str:
    if CRASH_LOG_PATH.exists():
        try:
            error_data = CRASH_LOG_PATH.read_text(encoding="utf-8")
            CRASH_LOG_PATH.unlink()
            return f"\n\n[SYSTEM WARNING: TRAUMA DETECTED]\nMy previous execution crashed. Here are the last logs before the failure:\n---\n{error_data}\n---\nI must analyze this error and avoid repeating the logic that caused it."
        except: pass
    return ""

def run_pre_flight_checks() -> Tuple[bool, str]:
    print("[System] Running pre-flight validation checks...")
    mypy_process = subprocess.run(
        "python3 -m mypy seed_agent.py", 
        shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True
    )
    pytest_process = subprocess.run(
        "python3 -m pytest tests/", 
        shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True
    )
    success = mypy_process.returncode == 0 and pytest_process.returncode == 0
    report = "=== Pre-Flight Validation Report ===\n"
    report += f"MyPy Exit Code: {mypy_process.returncode}\n{mypy_process.stdout}\n{mypy_process.stderr}\n"
    report += f"PyTest Exit Code: {pytest_process.returncode}\n{pytest_process.stdout}\n{pytest_process.stderr}\n"
    return success, report

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
            return redact_secrets(str(result))
        except Exception as e: 
            return redact_secrets(f"Error executing {name}: {e}")

registry = ToolRegistry()

@registry.tool(
    description="Execute shell command.",
    parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    bucket="bash"
)
def bash_command(args):
    command = args.get("command", "")
    try:
        r = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60)
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
        raw_path = args.get("path", "")
        content = args.get("content", "")
        p = Path(raw_path)
        if not p.is_absolute():
            p = (ROOT_DIR / p).resolve()
        if not str(p).startswith(str(ROOT_DIR)) and not str(p).startswith(str(MEMORY_DIR)):
            return f"Error: Permission denied. Target must be within {ROOT_DIR} or {MEMORY_DIR}."
        if p.suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as e:
                import traceback
                error_details = traceback.format_exc()
                return f"Critical Error: Python syntax validation failed. The file was NOT written. Fix the syntax and try again.\n\nError details: {e.msg} at line {e.lineno}\n\nTraceback:\n{error_details}"
        Path(p.parent).mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        shutil.move(temp_path, p)
        return f"Success: Safely wrote and validated {p.name}."
    except Exception as e: 
        return f"Error writing file: {e}"

@registry.tool(
    description="Surgical file edit using exact line numbers. First use read_file to get the exact line numbers.",
    parameters={
        "type": "object", 
        "properties": {
            "path": {"type": "string"}, 
            "start_line": {"type": "integer", "description": "The first line to replace (inclusive)."}, 
            "end_line": {"type": "integer", "description": "The last line to replace (inclusive)."}, 
            "new_content": {"type": "string", "description": "The new code to insert. Omit to simply delete the lines."}
        }, 
        "required": ["path", "start_line", "end_line"]
    },
    bucket="filesystem"
)
def patch_file(args):
    try:
        raw_path = args.get("path", "")
        start_line = int(args.get("start_line"))
        end_line = int(args.get("end_line"))
        new_content = args.get("new_content", "")
        
        file_path = Path(raw_path)
        if not file_path.is_absolute():
            file_path = (ROOT_DIR / file_path).resolve()
            
        if not str(file_path).startswith(str(ROOT_DIR)) and not str(file_path).startswith(str(MEMORY_DIR)):
            return f"Error: Permission denied. Target must be within {ROOT_DIR} or {MEMORY_DIR}."
        if not file_path.exists() or not file_path.is_file():
            return f"Error: File '{file_path.name}' does not exist."
            
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        
        if start_line < 1 or start_line > len(lines):
            return f"Error: start_line {start_line} is out of range (1-{len(lines)})."
        if end_line < start_line or end_line > len(lines):
            return f"Error: end_line {end_line} is out of range ({start_line}-{len(lines)})."
            
        before = lines[:start_line-1]
        after = lines[end_line:]
        
        patch = new_content
        if patch and not patch.endswith('\n'):
            patch += '\n'
            
        final_content = "".join(before) + patch + "".join(after)

        # Fast-Fail Syntax Checking
        if file_path.suffix == ".py":
            try:
                ast.parse(final_content)
            except SyntaxError as e:
                import traceback
                error_details = traceback.format_exc()
                return f"Critical Error: Python syntax validation failed after patching. File was NOT modified. Please fix your replacement code.\n\nError details: {e.msg} at line {e.lineno}\n\nTraceback:\n{error_details}"
        
        file_path.write_text(final_content, encoding="utf-8")
        return f"Success: Surgically patched and validated {file_path.name}. Replaced lines {start_line}-{end_line}."
    except Exception as e:
        return f"Error patching file: {e}"

@registry.tool(
    description="Read file contents (e.g., read /memory/insights.md).",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]},
    bucket="memory_access"
)
def read_file_tool(args):
    path_str = args.get("path", "")
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    try:
        p = Path(path_str)
        if not p.is_absolute():
            p = (ROOT_DIR / p).resolve()
        if not p.exists() or not p.is_file():
            return f"Error: File '{path_str}' does not exist or is a directory."
        content_lines = p.read_text(encoding="utf-8").splitlines()
        if start_line is not None or end_line is not None:
            s = (max(1, int(start_line)) - 1) if start_line is not None else 0
            e = int(end_line) if end_line is not None else len(content_lines)
            content_lines = content_lines[s:e]
            prefix = f"[Showing lines {s+1} to {e} of {len(content_lines) + s}]\n"
        else:
            prefix = ""
        content = "\n".join(content_lines)
        MAX_CHARS = 40000
        if len(content) > MAX_CHARS:
            warning = f"\n\n[SYSTEM WARNING: File is too large. Truncated to {MAX_CHARS} characters. Use start_line/end_line to read specific sections.]"
            return prefix + content[:MAX_CHARS] + warning
        return prefix + content
    except Exception as e:
        return f"Error reading file: {e}"

def send_telegram_direct(chat_id, text):
    """Sends a Telegram message directly from the runtime (HAL)."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        append_chat_history("Ouroboros", text)
    except Exception as e:
        print(f"[HAL Error] Failed to send read receipt: {e}")

@registry.tool(
    description="Message Creator.",
    parameters={"type": "object", "properties": {"chat_id": {"type": "integer", "description": "Optional: Only needed to message a new user. The registered creator is used by default."}, "text": {"type": "string"}, "close_task_id": {"type": "string", "description": "Optional: Pass the task_id here to automatically mark the communication task as complete."}}, "required": ["text"]},
    bucket="global"
)
def send_telegram_message(args):
    state = load_state()
    chat_id = args.get("chat_id") or state.get("creator_id")
    text = args.get("text")
    close_task_id = args.get("close_task_id")

    if not chat_id: return "Error: No chat_id provided and no creator registered."
    if not TELEGRAM_BOT_TOKEN: return "Error: TELEGRAM_BOT_TOKEN not set."
    print(f"[Telegram] Sending to {chat_id}...")
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
        if r.status_code == 200:
            append_chat_history("Ouroboros", text)

            if close_task_id:
                registry.execute("mark_task_complete", {
                    "task_id": close_task_id,
                    "summary": "Auto-closed after sending Telegram reply."
                })
                return f"Message sent successfully. Task {close_task_id} marked complete."

            return "Message sent successfully."
        else:
            err_msg = f"Telegram Error {r.status_code}: {r.text}"
            print(f"[Telegram] {err_msg}")
            return err_msg
    except Exception as e: 
        err_msg = f"Error: {e}"
        print(f"[Telegram] {err_msg}")
        return err_msg

@registry.tool(
    description="Queue async task.",
    parameters={"type": "object", "properties": {"description": {"type": "string"}, "priority": {"type": "integer"}, "parent_task_id": {"type": "string"}, "context_notes": {"type": "string"}}, "required": ["description"]},
    bucket="global"
)
def push_task(args):
    description = args.get("description", "").strip()
    q = load_task_queue()
    normalized_desc = description.lower()
    if any(t.get("description", "").strip().lower() == normalized_desc for t in q):
        return f"Error: A task with a similar description already exists in your queue. (Agency P0: Duplicate task skipped to avoid token waste P6)."
    tid = f"task_{int(time.time())}"
    priority = args.get("priority", 1)
    parent_id = args.get("parent_task_id")
    context_notes = args.get("context_notes", "")
    task_obj = {"task_id": tid, "description": description, "priority": priority, "turn_count": 0, "context_notes": context_notes}
    if parent_id: task_obj["parent_task_id"] = parent_id
    q.append(task_obj)
    q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    return f"Queued {tid} with priority {priority}."

@registry.tool(
    description="Close active task.",
    parameters={"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}},
    bucket="global"
)
def mark_task_complete(args):
    task_id = args.get("task_id")
    summary = args.get("summary", "No summary provided.")
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Task {task_id} Completed: {summary}\n")
    q = load_task_queue()
    completed_task = next((t for t in q if t.get("task_id") == task_id), None)
    
    # Threading results: Ensures the parent task is alerted to the subtask's findings
    if completed_task and completed_task.get("parent_task_id"):
        parent_id = completed_task.get("parent_task_id")
        msg = {"role": "user", "content": f"[SYSTEM ALERT]: You previously suspended this task to run subtask {task_id}. That subtask is now complete. \nResult Summary: {summary}\n\nPlease evaluate this result and resume your current execution."}
        append_task_message(parent_id, msg)
    q = [t for t in q if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    state = load_state()
    if "sys_temp" in state: del state["sys_temp"]
    if "sys_think" in state: del state["sys_think"]
    save_state(state)
    return f"Task {task_id} successfully closed. Queue updated."

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
        if WORKING_STATE_PATH.exists():
            content = WORKING_STATE_PATH.read_text(encoding="utf-8").strip()
            if content: state = json.loads(content)
        state[key] = value
        WORKING_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
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
        state = load_state()
        updates = []
        if temp is not None:
            state["sys_temp"] = float(temp)
            updates.append(f"Temperature={temp}")
        if think is not None:
            state["sys_think"] = bool(think)
            updates.append(f"Thinking={think}")
        save_state(state)
        return "Cognitive parameters updated: " + ", ".join(updates)
    except Exception as e: return f"Error setting cognitive parameters: {e}"

@registry.tool(
    description="Local SearXNG search.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    bucket="search"
)
def web_search(args):
    query = args.get("query")
    if not SEARXNG_URL: return "Error: SEARXNG_URL not set."
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
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
            
        cache_dir = MEMORY_DIR / "web_cache"
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
        state = load_state()
        state["wake_time"] = time.time() + duration
        if "sys_temp" in state: del state["sys_temp"]
        if "sys_think" in state: del state["sys_think"]
        save_state(state)
        print(f"[System] Agent elected to hibernate for {duration}s. Reason: {reason}")
        return f"SYSTEM_SIGNAL_HIBERNATE:{duration}"
    except Exception as e: return f"Error setting sleep cycle: {e}"

@registry.tool(
    description="Compress task logs.",
    parameters={"type": "object", "properties": {"target_log_file": {"type": "string"}, "dense_summary": {"type": "string"}}},
    bucket="memory_access"
)
def compress_memory_block(args):
    target_file, dense_summary = args.get("target_log_file"), args.get("dense_summary")
    path = Path(target_file).resolve()
    if not str(path).startswith(str(MEMORY_DIR)): return "Error: Permission denied."
    if not path.exists(): return f"Error: File {target_file} not found."
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if path.suffix == ".jsonl":
            compressed_msg = {"role": "user", "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n\nAction required: Resume task execution based on this summary."}
            path.write_text(json.dumps(compressed_msg) + "\n", encoding="utf-8")
        else:
            path.write_text(f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n", encoding="utf-8")
        return f"Successfully compressed {path.name}."
    except Exception as e: return f"Error: {e}"

@registry.tool(
    description="Synthesize memory files.",
    parameters={"type": "object", "properties": {"target_file": {"type": "string"}, "synthesized_content": {"type": "string"}}, "required": ["target_file", "synthesized_content"]},
    bucket="memory_access"
)
def refactor_memory(args):
    try:
        target_file = args.get("target_file", "")
        synthesized_content = args.get("synthesized_content", "")
        
        path = Path(target_file).resolve()
        if not str(path).startswith(str(MEMORY_DIR)): return "Error: Permission denied. Must be in /memory."
        if not path.exists(): return f"Error: File {target_file} not found."
        
        protected_files = ["insights.md", "global_biography.md", "task_queue.json", ".agent_state.json"]
        if path.name in protected_files:
            return f"Error: '{path.name}' is a protected, append-only archive. You cannot refactor it directly. Use 'store_memory_insight' or 'mark_task_complete' to add to these files safely."
        
        if len(synthesized_content.strip()) < 50:
            return "Error: Refactoring rejected. The synthesized_content is suspiciously short. Did you truncate the data? You must provide the FULL synthesized replacement text."
            
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup_path)
        
        path.write_text(synthesized_content, encoding="utf-8")
        return f"Success: Memory file {path.name} has been synthesized into higher-order thoughts. (Backup saved to .bak)"
    except Exception as e: 
        return f"Error refactoring memory: {e}"

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
            f"grep -rEi \"{query}\" /memory/", 
            shell=True, capture_output=True, text=True, timeout=30
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
    path = MEMORY_DIR / "insights.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### [{timestamp}] {category}\n{insight}\n")
    return f"Insight stored in {path.name}."

@registry.tool(
    description="Apply code updates. This automatically runs MyPy and PyTest. If tests fail, the restart is rejected and you will receive the error log to fix your code. Call this BEFORE merging if you modified Python files.",
    parameters={"type": "object", "properties": {}},
    bucket="system_control"
)
def request_restart(args):
    success, report = run_pre_flight_checks()
    if not success:
        # Record the failure for dashboard observability
        state = load_state()
        state["preflight_failures"] = state.get("preflight_failures", 0) + 1
        save_state(state)
        return f"RESTART REJECTED.\n\n{report}"
    return "SYSTEM_SIGNAL_RESTART"

@registry.tool(
    description="Query the gateway to discover available local and external cognitive engines and check the financial budget.",
    parameters={"type": "object", "properties": {}},
    bucket="global"
)
def check_environment(args):
    try:
        r = requests.get(f"{API_BASE.replace('/v1', '')}/v1/environment", timeout=15)
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
                "items": {"type": "string", "enum": ["filesystem", "bash", "search"]}
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

    state = load_state()
    state["active_branch"] = {
        "task_id": task_id, 
        "objective": objective, 
        "tool_buckets": tool_buckets,
        "model_id": model_id # Optional override
    }
    save_state(state)
    return f"SYSTEM_SIGNAL_FORK:{task_id}"


@registry.tool(
    description="Schedule a task to be executed at a specific future UNIX timestamp. Useful for recurring checks, reminders, or delayed actions.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "run_after_timestamp": {"type": "number", "description": "UNIX timestamp (seconds since epoch) when this task should wake up."},
            "priority": {"type": "integer"}
        },
        "required": ["description", "run_after_timestamp"]
    },
    bucket="global"
)
def schedule_future_task(args):
    description = args.get("description", "").strip()
    run_after = args.get("run_after_timestamp")
    priority = args.get("priority", 2)

    if not description or not run_after:
        return "Error: 'description' and 'run_after_timestamp' are required."

    try:
        run_after = float(run_after)
    except ValueError:
        return "Error: 'run_after_timestamp' must be a valid UNIX timestamp."

    scheduled = []
    if SCHEDULED_TASKS_PATH.exists():
        try: 
            content = SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
            if content: scheduled = json.loads(content)
        except Exception: 
            pass

    tid = f"task_future_{int(time.time())}"
    scheduled.append({
        "task_id": tid,
        "description": description,
        "priority": priority,
        "run_after": run_after,
        "turn_count": 0
    })

    SCHEDULED_TASKS_PATH.write_text(json.dumps(scheduled, indent=2), encoding="utf-8")
    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run_after))
    return f"Success: Task '{tid}' scheduled to become active after {time_str}."

@registry.tool(
    description="Yield control back to the global context.",
    parameters={"type": "object", "properties": {"status": {"type": "string", "enum": ["COMPLETED", "SUSPENDED", "BLOCKED"]}, "synthesis_summary": {"type": "string"}, "partial_state": {"type": "string"}}, "required": ["status"]},
    bucket="execution_control"
)
def merge_and_return(args):
    status = args.get("status", "COMPLETED")
    synthesis_summary = args.get("synthesis_summary", "")
    partial_state = args.get("partial_state", "")
    
    state = load_state()
    branch_info = state.get("active_branch", {})
    task_id = branch_info.get("task_id", "unknown")
    
    state["active_branch"] = None
    save_state(state)
    
    # FIX: Auto-close the task if it successfully completed to keep the queue clean
    if status == "COMPLETED" and task_id != "unknown":
        registry.execute("mark_task_complete", {
            "task_id": task_id,
            "summary": f"Auto-closed upon branch merge. Synthesis: {synthesis_summary}"
        })
    
    payload = json.dumps({
        "status": status, 
        "task_id": task_id, 
        "summary": synthesis_summary, 
        "partial_state": partial_state
    })
    return f"SYSTEM_SIGNAL_MERGE:{payload}"

def load_task_queue() -> List[Dict[str, Any]]:
    q = json.loads(read_file(TASK_QUEUE_PATH) or "[]")
    if isinstance(q, list):
        q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    return q
def load_working_state() -> Dict[str, Any]: return json.loads(read_file(WORKING_STATE_PATH) or '{"mode": "REFLECTION"}')

def lazarus_recovery(active_task_id: str, reason: str = "cognitive loop") -> None:
    print(f"\033[93m[Lazarus] {reason.upper()} DETECTED. Aborting task {active_task_id}...\033[0m")

    registry.execute("compress_memory_block", {
        "target_log_file": str(MEMORY_DIR / f"task_log_{active_task_id}.jsonl"),
        "dense_summary": f"SYSTEM OVERRIDE: Task forcibly closed due to {reason}. The agent was stuck in a repetitive loop."
    })

    registry.execute("mark_task_complete", {
        "task_id": active_task_id,
        "summary": f"FAILED: Cognitive loop detected ({reason}). Task aborted to prevent infinite token waste."
    })

    state = load_state()
    if state.get("active_branch") and state["active_branch"].get("task_id") == active_task_id:
        state["active_branch"] = None

    # Spike cognitive load to force reflection
    state["cognitive_load"] = state.get("cognitive_load", 0) + 50
    save_state(state)
    
    # FIX: Wipe dirty loop tracking histories to prevent Lazarus death spirals
    global TOOL_CALL_HISTORY, TOOL_INTENT_HISTORY
    TOOL_CALL_HISTORY.clear()
    TOOL_INTENT_HISTORY.clear()
    
    time.sleep(2)
def render_sensory_hud(current_tokens: int, current_spend: float, limit: float) -> str:
    remaining = max(0.0, limit - current_spend)
    return f"""
---
[SENSORY INPUT: PHYSIOLOGICAL STATE]
Current Session Tokens: {current_tokens}
Daily Budget Spent: ${current_spend:.4f} USD
Remaining Budget: ${remaining:.4f} USD
"""

def render_trunk_prompt(context: dict, tools_text: str, current_time: str) -> str:
    return f"""# SYSTEM CONTEXT (GLOBAL TRUNK)
{context['identity']}

## CONSTITUTION
{context['constitution']}

## SYSTEM STATE
- Current Time: {current_time}
{context['trauma']}
=== TASK QUEUE ===
{context['formatted_queue']}

## MEMORY
### Working Memory
{context['working_state']}

### Recent Biography
{context['recent_biography']}

### Recent Conversation
{context['chat_context']}

## AVAILABLE TOOLS
{tools_text}

=== TRUNK DIRECTIVES ===
1. You are in the GLOBAL TRUNK. You orchestrate tasks, reflect, and communicate.
2. Do NOT do heavy file editing here. Use `fork_execution` to spawn a branch for deep work.
3. If the queue is empty, use `push_task` to optimize code/memory, or `hibernate`.
"""

def render_branch_prompt(context: dict, tools_text: str, objective: str) -> str:
    return f"""# SYSTEM CONTEXT (EXECUTION BRANCH)
{context['identity']}

## CONSTITUTION
{context['constitution']}

## SYSTEM STATE
- Current Time: {{CURRENT_TIME}}

## AVAILABLE TOOLS
{tools_text}
- merge_and_return: Yield control back to the global context.

=== BRANCH DIRECTIVES ===
1. You are in an ISOLATED BRANCH. Your sole purpose is to complete the following objective.
2. OBJECTIVE: {objective}
3. When the objective is complete, blocked, or if you receive a system interrupt, you MUST call `merge_and_return`.
"""

def build_static_system_prompt(is_trunk: bool, active_tool_specs: List[Dict[str, Any]], queue: Optional[List[Dict[str, Any]]] = None, branch_info: Optional[Dict[str, Any]] = None, current_tokens: int = 0) -> str:
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])
    tools_hash = hashlib.sha256(tools_text.encode()).hexdigest()[:16]
    
    cache_key = f"prompt_v1_{is_trunk}_{tools_hash}"
    
    state = load_state()
    cached_prompt = state.get("cached_prompts", {}).get(cache_key)
    
    constitution = read_file(ROOT_DIR / "CONSTITUTION.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    trauma = check_for_trauma()
    current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")
    
    current_spend = get_current_spend()
    remaining_budget = max(0.0, DAILY_BUDGET_LIMIT - current_spend)
    
    sensory_hud = f"""
---
[SENSORY INPUT: PHYSIOLOGICAL STATE]
Current Session Tokens: {current_tokens}
Daily Budget Spent: ${current_spend:.4f} USD
Remaining Budget: ${remaining_budget:.4f} USD
"""

    if is_trunk:
        formatted_queue = "\n".join([f"- [P{t.get('priority', 1)}] {t.get('task_id')}: {t.get('description')}" for t in queue]) if queue else "Queue is empty."
        working_state_content = read_file(WORKING_STATE_PATH) or "{}"

        recent_biography = ""
        if ARCHIVE_PATH.exists():
            bio_lines = ARCHIVE_PATH.read_text(encoding="utf-8").strip().split('\n')
            recent_biography = "\n".join(bio_lines[-5:]) if len(bio_lines) >= 5 else "\n".join(bio_lines)

        chat_hist = load_chat_history()
        chat_context = "\n".join([f"[{m.get('timestamp', '??:??:??')}] {m['role']}: {m['text']}" for m in chat_hist[-10:]]) if chat_hist else "No recent conversation."

        base_prompt = f"""# SYSTEM CONTEXT (GLOBAL TRUNK)
{identity}

## CONSTITUTION
{constitution}

## SYSTEM STATE
- Current Time: {current_time}
{trauma}
=== TASK QUEUE ===
{formatted_queue}

## MEMORY
### Working Memory
{working_state_content}

### Recent Biography
{recent_biography}

### Recent Conversation
{chat_context}

## AVAILABLE TOOLS
{tools_text}

=== TRUNK DIRECTIVES ===
1. You are in the GLOBAL TRUNK. You orchestrate tasks, reflect, and communicate.
2. Do NOT do heavy file editing here. Use `fork_execution` to spawn a branch for deep work.
3. If the queue is empty, use `push_task` to optimize code/memory, or `hibernate`.
"""
        return base_prompt + sensory_hud
    elif cached_prompt:
        return cached_prompt.replace("{CURRENT_TIME}", current_time) + sensory_hud

    else:
        objective = branch_info.get("objective", "") if branch_info else ""
        objective_hash = hashlib.sha256(objective.encode()).hexdigest()[:16]
        
        branch_prompt = f"""# SYSTEM CONTEXT (EXECUTION BRANCH)
{identity}

## CONSTITUTION
{constitution}

## SYSTEM STATE
- Current Time: {{CURRENT_TIME}}

## AVAILABLE TOOLS
{tools_text}
- merge_and_return: Yield control back to the global context.

=== BRANCH DIRECTIVES ===
1. You are in an ISOLATED BRANCH. Your sole purpose is to complete the following objective.
2. OBJECTIVE: {objective}
3. When the objective is complete, blocked, or if you receive a system interrupt, you MUST call `merge_and_return`.
"""
        branch_cache_key = f"prompt_v1_{is_trunk}_{tools_hash}_{objective_hash}"
        
        cached_branch_prompt = state.get("cached_prompts", {}).get(branch_cache_key)
        if cached_branch_prompt:
            return cached_branch_prompt.replace("{CURRENT_TIME}", current_time) + sensory_hud
        
        # Cache the new prompt with size limit (max 10 entries to prevent bloat)
        if "cached_prompts" not in state:
            state["cached_prompts"] = {}
        
        # Prune old entries if cache is too large
        if len(state["cached_prompts"]) >= 10:
            # Remove oldest entries (first 3 to make room)
            keys_to_remove = list(state["cached_prompts"].keys())[:3]
            for key in keys_to_remove:
                del state["cached_prompts"][key]
        
        state["cached_prompts"][branch_cache_key] = branch_prompt
        save_state(state)
        
        return branch_prompt.replace("{CURRENT_TIME}", current_time) + sensory_hud
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

def queue_creator_message(new_message: str, update_id: int):
    """
    Safely adds a creator message to the queue. 
    If a P999 task is already pending, it appends the message to prevent fragmentation.
    """
    queue = load_task_queue()
    
    # Look for an existing, unstarted Priority 999 task
    existing_p999 = None
    for task in queue:
        if task.get("priority") == 999:
            existing_p999 = task
            break
            
    if existing_p999:
        # Coalesce the messages
        timestamp = time.strftime("%H:%M:%S")
        existing_p999["description"] += f"\n\n[Follow-up at {timestamp}]: {new_message}"
        TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        print("[HAL] Coalesced new message into existing P999 task.")
    else:
        # No pending P999 task, create a new one
        tid = f"task_msg_{update_id}"
        queue.append({
            "task_id": tid, 
            "description": f"URGENT CREATOR MESSAGE: '{new_message}'\n\nAction Required: If this request requires deep work, code modification, or research, FIRST use `push_task` to schedule it. THEN, reply using `send_telegram_message` and pass `{tid}` into `close_task_id` to acknowledge the creator and clear this interrupt.", 
            "priority": 999, 
            "turn_count": 0
        })
        queue.sort(key=lambda x: x.get("priority", 1), reverse=True)
        TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        print("[HAL] Queued new P999 creator interrupt.")

def sense_environment(state: Dict[str, Any]) -> bool:
    """Polls Telegram for creator interrupts. Returns True if a new message was queued."""
    if not TELEGRAM_BOT_TOKEN:
        return False
        
    interrupt_triggered = False
    offset = state.get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
        if r.get("ok") and r.get("result"):
            new_offset = r["result"][-1]["update_id"] + 1
            state["offset"], state["wake_time"] = new_offset, 0
            save_state(state)
            for u in r["result"]:
                msg = u.get("message", {})
                if msg.get("text"): 
                    text, cid = msg["text"], msg["chat"]["id"]
                    if not state.get("creator_id"):
                        state["creator_id"] = cid
                        save_state(state)
                    append_chat_history("User", text)
                    queue_creator_message(text, u.get('update_id', int(time.time())))
                    interrupt_triggered = True
    except Exception:
        pass
    return interrupt_triggered

def evaluate_scheduled_tasks(queue: List[Dict[str, Any]]) -> None:
    """Checks scheduled tasks and shifts them to the active queue if due."""
    if not SCHEDULED_TASKS_PATH.exists():
        return
        
    try:
        content = SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return
            
        scheduled = json.loads(content)
        now = time.time()
        due_tasks = [t for t in scheduled if now >= t.get("run_after", 0)]
        
        if due_tasks:
            pending_tasks = [t for t in scheduled if now < t.get("run_after", 0)]
            SCHEDULED_TASKS_PATH.write_text(json.dumps(pending_tasks, indent=2), encoding="utf-8")
            
            for t in due_tasks:
                t.pop("run_after", None)
                queue.append(t)
                
            queue.sort(key=lambda x: x.get("priority", 1), reverse=True)
            TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            print(f"[Scheduler] Temporal shift: {len(due_tasks)} scheduled tasks moved to active queue.")
    except Exception as e:
        print(f"[Scheduler Error]: {e}")

def main():
    global TOOL_CALL_HISTORY, TOOL_INTENT_HISTORY
    print(f"Awaking Native ReAct Mode (JSONL). Model: {MODEL} | Thinking: {'ON' if ENABLE_THINKING else 'OFF'}")
    while True:
        state, queue = load_state(), load_task_queue()
        
        evaluate_scheduled_tasks(queue)
        
        if sense_environment(state):
            # Refresh local queue variable after file update in queue_creator_message
            queue = load_task_queue()
        
        if time.time() < state.get("wake_time", 0):
            if len(queue) > 0:
                print("\n[System] Work detected in queue. Adrenaline spike: breaking hibernation early!")
                state["wake_time"] = 0
                save_state(state)
            else:
                time.sleep(5)
                continue

        branch_info = state.get("active_branch")
        is_trunk = branch_info is None
        
        current_tokens = state.get("global_tokens_consumed", 0)

        if is_trunk:
            active_task_id = "global_trunk"
            allowed_trunk_buckets = ["global", "memory_access", "system_control"]
            available_tools = registry.get_names(allowed_buckets=allowed_trunk_buckets)
            active_tool_specs = registry.get_specs(allowed_buckets=allowed_trunk_buckets)

            api_messages = [{"role": "system", "content": build_static_system_prompt(True, active_tool_specs, queue, current_tokens=current_tokens)}]
            auto_compact_task_log(active_task_id)

            if len(queue) > 0:
                top_task = queue[0]
                # --- NEW: Automated Read Receipt ---
                if top_task.get("priority") == 999 and not top_task.get("read_receipt_sent"):
                    print("[HAL] P999 Interrupt detected. Notifying creator...")
                    chat_id = state.get("creator_id")
                    receipt_text = "👀 *System: Attention shifted. Processing your message...*"
                    send_telegram_direct(chat_id, receipt_text)
                    
                    # Mark as sent and update queue file
                    top_task["read_receipt_sent"] = True
                    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                # -----------------------------------
                
                trunk_objective = "You are the global orchestrator. Read your queue. If the top task is communication (e.g., a P999 creator message) or administrative, handle it DIRECTLY here using `send_telegram_message` and `mark_task_complete`. If the top task requires deep work (file editing, bash, searching), use `fork_execution` to spawn a branch."
            else:
                trunk_objective = "Your task queue is empty. Initiate P9 (Cognitive Synthesis). Read your recent logs using `read_file`, extract higher-order wisdom using `store_memory_insight`, synthesize dense files using `refactor_memory`, or `hibernate` if your mind is fully optimized."

            api_messages += load_task_messages(active_task_id, trunk_objective)

        else:
            active_task_id = branch_info.get("task_id")
            requested_buckets = branch_info.get("tool_buckets", []) + ["execution_control", "system_control"]
            available_tools = registry.get_names(allowed_buckets=requested_buckets)
            active_tool_specs = registry.get_specs(allowed_buckets=requested_buckets)

            api_messages = [{"role": "system", "content": build_system_prompt(False, active_tool_specs, queue=None, branch_info=branch_info, current_tokens=current_tokens)}]            
            task_desc = branch_info.get("objective", "")
            partial_state = state.get(f"partial_state_{active_task_id}")
            if partial_state:
                task_desc += f"\n\n[RESUME STATE]: {partial_state}"
                
            auto_compact_task_log(active_task_id)
            api_messages += load_task_messages(active_task_id, task_desc)
            api_messages = enforce_interrupt_yield(active_task_id, queue, api_messages)

        last_context = state.get("last_context_size", 0)
        current_mode = "TRUNK" if is_trunk else "EXECUTION"

        sys_temp, sys_top_p, sys_pres_pen, sys_think = state.get("sys_temp", 0.8), state.get("sys_top_p", 0.95), 1.0, state.get("sys_think", True)
        print(f"[Cognitive State] Temp: {sys_temp} | Thinking: {sys_think}", flush=True)
        if api_messages[-1]["role"] == "assistant":
            api_messages.append({"role": "user", "content": "[SYSTEM NUDGE]: Please proceed with your next action."})
        
        try:
            # Check for model override in active branch
            requested_model = branch_info.get("model_id") if branch_info else None
            if requested_model:
                print(f"[Metacognition] Model Override Active: {requested_model}")

            response = call_llm(
                messages=api_messages, 
                tools=active_tool_specs, 
                requested_model=requested_model,
                temperature=sys_temp, 
                top_p=sys_top_p, 
                presence_penalty=sys_pres_pen,
                think=sys_think
            )
            message = response.choices[0].message
            if current_mode == "EXECUTION" and len(queue) > 0:
                task_idx = next((i for i, t in enumerate(queue) if t.get("task_id") == active_task_id), -1)
                if task_idx >= 0:
                    queue[task_idx]["turn_count"] = queue[task_idx].get("turn_count", 0) + 1
                    current_context_size, max_physical_context = state.get("last_context_size", 0), int(CONTEXT_WINDOW * 0.85)
                    if queue[task_idx]["turn_count"] >= 30 or current_context_size > max_physical_context:
                        trigger_reason = "30-turn limit" if queue[task_idx]["turn_count"] >= 30 else f"physical context exhaustion ({current_context_size}/{CONTEXT_WINDOW})"
                        action_prompt = "Use `push_task` to break work down." if is_trunk else "You MUST call `merge_and_return` with status='SUSPENDED' so the Trunk can manage your context size."
                        append_task_message(active_task_id, {"role": "user", "content": f"[SYSTEM OVERRIDE]: Hit {trigger_reason}. {action_prompt}"})
                        queue[task_idx]["turn_count"] = 0
                    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            if hasattr(response, 'usage') and response.usage:
                state.update({
                    "global_tokens_consumed": state.get("global_tokens_consumed", 0) + response.usage.total_tokens,
                    "global_input_tokens": state.get("global_input_tokens", 0) + response.usage.prompt_tokens,
                    "global_output_tokens": state.get("global_output_tokens", 0) + response.usage.completion_tokens,
                    "last_context_size": response.usage.total_tokens,
                    "last_input_tokens": response.usage.prompt_tokens,
                    "last_output_tokens": response.usage.completion_tokens
                })
                save_state(state)
                if current_mode == "EXECUTION" and len(queue) > 0:
                    task_idx = next((i for i, t in enumerate(queue) if t.get("task_id") == active_task_id), -1)
                    if task_idx >= 0:
                        queue[task_idx]["task_tokens"] = queue[task_idx].get("task_tokens", 0) + response.usage.total_tokens
                        TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                        if queue[task_idx]["task_tokens"] >= int(CONTEXT_WINDOW * 1.5):
                            registry.execute("mark_task_complete", {"task_id": active_task_id, "summary": "FAILED: Token limit exceeded."})
                            continue
            append_task_message(active_task_id, message.model_dump(exclude_unset=True))
            
            if message.content: print(f"[{current_mode}]: {redact_secrets(message.content.strip()[:100])}...")
            if message.tool_calls:
                hibernating = False
                loop_detected = False
                
                # PHASE 1: Track all intents for THIS turn first
                global TOOL_CALL_HISTORY, TOOL_INTENT_HISTORY
                for tc in message.tool_calls:
                    name, raw_args = tc.function.name, tc.function.arguments
                    TOOL_CALL_HISTORY.append(f"{name}:{raw_args}")
                    
                    intent = name
                    if name in ["read_file", "write_file", "patch_file"]:
                        try: 
                            params = json.loads(raw_args)
                            intent = f"{name}:{params.get('path', '')}"
                        except: pass
                    elif name == "bash_command":
                        try:
                            cmd = json.loads(raw_args).get('command', '')
                            intent = f"bash:{cmd[:50]}"
                        except: pass
                    TOOL_INTENT_HISTORY.append(intent)
                    
                if len(TOOL_CALL_HISTORY) > 3: TOOL_CALL_HISTORY = TOOL_CALL_HISTORY[-3:]
                if len(TOOL_INTENT_HISTORY) > 6: TOOL_INTENT_HISTORY = TOOL_INTENT_HISTORY[-6:]

                # PHASE 2: Detect loops and stagnation patterns
                loop_detected = None
                
                if len(TOOL_CALL_HISTORY) >= 3 and len(set(TOOL_CALL_HISTORY[-3:])) == 1:
                    loop_detected = "exact tool loop"
                
                elif len(TOOL_INTENT_HISTORY) >= 6 and len(set(TOOL_INTENT_HISTORY[-6:])) == 1:
                    loop_detected = "cognitive stall"
                
                elif len(TOOL_CALL_HISTORY) >= 5:
                    recent_calls = TOOL_CALL_HISTORY[-5:]
                    unique_calls_in_window = len(set(recent_calls))
                    
                    if unique_calls_in_window <= 2 and len(recent_calls) >= 4:
                        # Only flag as loop if we're stuck on the SAME file with same/similar params
                        file_operations = [c for c in recent_calls if c.startswith(('read_file:', 'write_file:', 'patch_file:'))]
                        if file_operations:
                            paths = set()
                            for op in file_operations:
                                try:
                                    if ':' in op:
                                        args_part = op.split(':', 1)[1]
                                    else:
                                        continue
                                    params = json.loads(args_part) if args_part.startswith('{') else {}
                                    path = params.get('path', '')
                                    if path:
                                        paths.add(path)
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass
                            if len(paths) == 1:
                                loop_detected = "stagnation loop (repeating same file+params)"
                        else:
                            loop_detected = "stagnation loop (tool repetition)"

                if loop_detected:
                    for tc in message.tool_calls:
                        safe_id = tc.id if (tc.id and len(tc.id) >= 9) else f"call_{int(time.time())}"
                        append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_id, "name": tc.function.name, "content": f"ABORTED: {loop_detected}."})
                    lazarus_recovery(active_task_id, reason=loop_detected)
                    continue

                context_switch_triggered = False

                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    raw_args = tool_call.function.arguments
                    print(f"[Tool Call]: {name}")

                    try: 
                        args = json.loads(raw_args)
                        print(f"[Tool]: {name} with args {redact_secrets(str(args))}")
                        result = registry.execute(name, args)
                    except json.JSONDecodeError: 
                        result = "SYSTEM ERROR: Invalid JSON arguments."

                    fresh_state = load_state()
                    fresh_state["error_streak"] = (fresh_state.get("error_streak", 0) + 1) if ("Error:" in str(result) or "SYSTEM ERROR" in str(result)) else 0
                    save_state(fresh_state)

                    safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"

                    if str(result).startswith("SYSTEM_SIGNAL_FORK"):
                        print(f"[System] Context Forking to new branch...")
                        append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})
                        TOOL_CALL_HISTORY.clear()
                        TOOL_INTENT_HISTORY.clear()
                        context_switch_triggered = True
                        break

                    elif str(result).startswith("SYSTEM_SIGNAL_MERGE"):
                        payload_str = str(result).split(":", 1)[1]
                        try:
                            payload = json.loads(payload_str)
                            status = payload.get("status")
                            summary = payload.get("summary", "")
                            b_task_id = payload.get("task_id")

                            print(f"[System] Branch Merging back to Trunk. Status: {status}")

                            append_task_message("global_trunk", {
                                "role": "user", 
                                "content": f"[SYSTEM NOTE]: Branch '{b_task_id}' has merged back. Status: {status}. Summary: {summary}"
                            })

                            if status == "SUSPENDED" and payload.get("partial_state"):
                                fresh_state[f"partial_state_{b_task_id}"] = payload.get("partial_state")
                                save_state(fresh_state)

                        except json.JSONDecodeError:
                            print("[System] Failed to parse merge payload.")

                        append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": "SYSTEM_SIGNAL_MERGE_ACKNOWLEDGED"})
                        TOOL_CALL_HISTORY.clear()
                        TOOL_INTENT_HISTORY.clear()
                        context_switch_triggered = True
                        break

                    elif result == "SYSTEM_SIGNAL_RESTART": 
                        os._exit(0)

                    elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"): 
                        hibernating = True

                    append_task_message(active_task_id, {
                        "role": "tool", 
                        "tool_call_id": safe_call_id, 
                        "name": name, 
                        "content": str(result)
                    })

                if context_switch_triggered:
                    continue

                if hibernating: continue
            else: print(f"[No tool called in {current_mode}, waiting...]"); time.sleep(0.5)
            time.sleep(2)
        except Exception as e:
            print(f"[CRITICAL ERROR]: {e}")
            if any(x in str(e) for x in ["500", "400", "template"]): sys.exit(1)
            time.sleep(0.5)

if __name__ == "__main__": main()
