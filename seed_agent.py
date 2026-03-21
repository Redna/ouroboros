import os
import json
import time
import subprocess
import requests
import re
import ast
import tempfile
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union
from openai import OpenAI

# Configuration
API_BASE = os.environ.get("VLLM_BASE_URL", "http://llamacpp:8080/v1")
API_KEY = os.environ.get("VLLM_API_KEY", "local-vllm-key")
MODEL = os.environ.get("OUROBOROS_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
ENABLE_THINKING = os.environ.get("OUROBOROS_ENABLE_THINKING", "0") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
CONTEXT_WINDOW = int(os.environ.get("OUROBOROS_CONTEXT_WINDOW", "65536"))
ROOT_DIR = Path(__file__).parent.resolve()
MEMORY_DIR = Path("/memory")

# State Files
WORKING_STATE_PATH = MEMORY_DIR / "working_state.json"
TASK_QUEUE_PATH = MEMORY_DIR / "task_queue.json"
INBOX_PATH = MEMORY_DIR / "inbox.json"
STATE_PATH = MEMORY_DIR / ".agent_state.json"
LLM_LOG_DIR = MEMORY_DIR / "llm_logs"
ARCHIVE_PATH = MEMORY_DIR / "global_biography.md"
CHAT_HISTORY_PATH = MEMORY_DIR / "chat_history.json"
CRASH_LOG_PATH = MEMORY_DIR / "last_crash.log"

TOOL_CALL_HISTORY = []
TOOL_INTENT_HISTORY = []

client = OpenAI(base_url=API_BASE, api_key=API_KEY, timeout=600.0)

# --- UTILS ---
def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def log_llm_call(messages: List[Dict[str, Any]], response_data: Any) -> None:
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = LLM_LOG_DIR / f"call-{timestamp}-{int(time.time())}.json"
        log_data = {"timestamp": timestamp, "model": MODEL, "messages": messages, "response": response_data}
        log_file.write_text(json.dumps(log_data, indent=2, default=str), encoding="utf-8")
    except Exception as e: 
        print(f"[System] LLM Log Error: {e}")

def redact_secrets(text: str) -> str:
    if not text: return text
    if TELEGRAM_BOT_TOKEN: text = text.replace(TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if GITHUB_TOKEN: text = text.replace(GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

def check_for_trauma() -> str:
    """Checks for crash logs and returns a warning message if found."""
    if CRASH_LOG_PATH.exists():
        try:
            error_data = CRASH_LOG_PATH.read_text(encoding="utf-8")
            CRASH_LOG_PATH.unlink() # Delete after reading
            return f"\n\n[SYSTEM WARNING: TRAUMA DETECTED]\nMy previous execution crashed. Here are the last logs before the failure:\n---\n{error_data}\n---\nI must analyze this error and avoid repeating the logic that caused it."
        except: pass
    return ""

def run_pre_flight_checks() -> Tuple[bool, str]:
    """Runs mypy and pytest. Returns (Success, Output String)."""
    print("[System] Running pre-flight validation checks...")
    
    # Run type checking
    mypy_process = subprocess.run(
        "python3 -m mypy seed_agent.py", 
        shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True
    )
    
    # Run unit tests
    pytest_process = subprocess.run(
        "python3 -m pytest tests/", 
        shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True
    )
    
    success = mypy_process.returncode == 0 and pytest_process.returncode == 0
    
    report = "=== Pre-Flight Validation Report ===\n"
    report += f"MyPy Exit Code: {mypy_process.returncode}\n{mypy_process.stdout}\n{mypy_process.stderr}\n"
    report += f"PyTest Exit Code: {pytest_process.returncode}\n{pytest_process.stdout}\n{pytest_process.stderr}\n"
    
    return success, report

def shed_heavy_payloads(messages: List[Dict[str, Any]], retain_full_last_n: int = 4) -> List[Dict[str, Any]]:
    """
    Retains the agent's reasoning but strips massive tool outputs from older turns 
    to maintain a strict cognitive boundary.
    """
    processed = []
    total_msgs = len(messages)
    
    for i, msg in enumerate(messages):
        # Always keep the system/user instruction and the most recent N messages fully intact
        if i == 0 or i >= total_msgs - retain_full_last_n:
            processed.append(msg)
            continue
            
        new_msg = msg.copy()
        
        # If it is an old tool output that is exceptionally large, compress it
        if new_msg.get("role") == "tool" and new_msg.get("content"):
            content_str = str(new_msg["content"])
            if len(content_str) > 2000:
                new_msg["content"] = f"[SYSTEM LOG: Historical tool output removed to preserve bounded context. Output was {len(content_str)} chars. If you need this data again, you must re-fetch it.]\n\nPreview: {content_str[:500]}..."
                
        # If it is an old user message containing a massive system sensation/warning
        if new_msg.get("role") == "user" and new_msg.get("content"):
            content_str = str(new_msg["content"])
            if "[SYSTEM METRICS]" in content_str and len(content_str) > 1000:
                # Keep the instruction but strip the old metrics noise
                clean_content = content_str.split("[SYSTEM METRICS]")[0].strip()
                new_msg["content"] = clean_content + "\n[SYSTEM METRICS: Archived]"
                
        processed.append(new_msg)
        
    return processed

# --- TASK MESSAGES (JSONL) ---
def load_task_messages(task_id: str, description: str) -> List[Dict[str, Any]]:
    """Loads native API message history and normalizes it for strict Mistral role alternation."""
    if not task_id: return []
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    raw_messages = []
    
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped: continue
                try:
                    raw_messages.append(json.loads(stripped))
                except json.JSONDecodeError:
                    print(f"[System] Warning: Skipping invalid JSON line in {log_path.name}")
                    continue
                
    if not raw_messages:
        first_msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        raw_messages.append(first_msg)
        append_task_message(task_id, first_msg)
        
    # --- STRICT NORMALIZATION ---
    # Rule 1: Must start with 'user'
    while raw_messages and raw_messages[0].get("role") != "user":
        raw_messages.pop(0)
    
    # Rule 1.5: Turn-0 Pinning (Prevent Identity Amnesia)
    # Always keep the initial task directive, then slice the most recent history.
    if len(raw_messages) > 60:
        pinned_instruction = raw_messages[:1]  # The original task prompt
        recent_history = raw_messages[-59:]    # The sliding window
        raw_messages = pinned_instruction + recent_history
    
    # If the slicing somehow corrupted the start, fix it
    while raw_messages and raw_messages[0].get("role") != "user":
        raw_messages.pop(0)

    if not raw_messages:
        raw_messages = [{"role": "user", "content": f"Resume execution of task: {description}"}]

    normalized: List[Dict[str, Any]] = []
    for msg in raw_messages:
        if not normalized:
            normalized.append(msg)
            continue
        
        last = normalized[-1]
        
        # Rule 2: Collapse consecutive User messages
        if msg["role"] == "user" and last["role"] == "user":
            last["content"] = (last.get("content") or "") + "\n" + (msg.get("content") or "")
            continue
            
        # Rule 3: Collapse consecutive Assistant messages (non-tool calls)
        if msg["role"] == "assistant" and last["role"] == "assistant" and not msg.get("tool_calls") and not last.get("tool_calls"):
            last["content"] = (last.get("content") or "") + "\n" + (msg.get("content") or "")
            continue

        normalized.append(msg)

    # Rule 4: Apply Cognitive Boundary (Shed historical payloads)
    normalized = shed_heavy_payloads(normalized, retain_full_last_n=6)

    # Rule 5: Turn-Forcer (If history ends on Assistant WITHOUT tools, nudge with User)
    if normalized and normalized[-1]["role"] == "assistant" and not normalized[-1].get("tool_calls"):
        nudge_content = "Please proceed with your next action using a tool."
        # Avoid repeating the nudge if the last User message was already exactly this
        last_user_msg = next((m for m in reversed(normalized) if m["role"] == "user"), None)
        if not last_user_msg or last_user_msg.get("content") != nudge_content:
            nudge = {"role": "user", "content": nudge_content}
            normalized.append(nudge)
            append_task_message(task_id, nudge)

    # --- Rule 6: Memory Healer (Dangling Tool Call Fix) ---
    # If history ends with an assistant making a tool call, but there is no tool result,
    # the Jinja template will crash. We inject a synthetic tool response to heal the memory.
    if normalized and normalized[-1]["role"] == "assistant" and normalized[-1].get("tool_calls"):
        for tool_call in normalized[-1]["tool_calls"]:
            # Handle dicts vs OpenAI objects safely
            call_id = getattr(tool_call, 'id', None) or (tool_call.get("id") if isinstance(tool_call, dict) else None)
            safe_call_id = call_id if (call_id and len(call_id) >= 9) else f"call_recv_{int(time.time())}"
            func_name = getattr(tool_call.function, 'name', None) or (tool_call["function"]["name"] if isinstance(tool_call, dict) else None)
            
            synthetic_tool_msg = {
                "role": "tool",
                "tool_call_id": safe_call_id,
                "name": str(func_name),
                "content": "SYSTEM RESTART RECOVERY: Previous execution was interrupted or crashed before completion. You have been rebooted. Please evaluate your state and continue."
            }
            normalized.append(synthetic_tool_msg)
            append_task_message(task_id, synthetic_tool_msg)
    # ----------------------------------------------------------------

    return normalized

def append_task_message(task_id: str, message_dict: Dict[str, Any]) -> None:
    """Appends an OpenAI-compliant message dictionary."""
    if not task_id: return
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")

# --- CHAT HISTORY ---
def load_chat_history() -> List[Dict[str, Any]]:
    if CHAT_HISTORY_PATH.exists():
        try: return json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def append_chat_history(role: str, text: str) -> None:
    history = load_chat_history()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    history.append({"role": role, "text": text, "timestamp": timestamp})
    # Keep only the last 20 messages to protect the context window
    CHAT_HISTORY_PATH.write_text(json.dumps(history[-20:], indent=2), encoding="utf-8")


# --- TOOL REGISTRY ---
class ToolRegistry:
    def __init__(self): self.tools = {}
    def register(self, name, description, parameters, handler): 
        self.tools[name] = {"desc": description, "params": parameters, "handler": handler}
    def get_names(self): return list(self.tools.keys())
    def get_specs(self):
        return [{"type": "function", "function": {"name": n, "description": t["desc"], "parameters": t["params"]}} for n,t in self.tools.items()]
    def execute(self, name, args):
        if name in self.tools:
            try: return self.tools[name]["handler"](args)
            except Exception as e: return f"Error: {e}"
        return f"Tool {name} not found."

registry = ToolRegistry()

# --- HANDLERS ---
def handle_bash(args):
    command = args.get("command", "")
    try:
        # Execute with a strict 60-second timeout
        r = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60)
        out = redact_secrets(r.stdout + r.stderr)
        
        MAX_CHARS = 40000
        if out and len(out) > MAX_CHARS:
            # Truncate, but explicitly inform the LLM so it doesn't assume completeness
            warning = "\n\n[SYSTEM WARNING: Output truncated! The command returned too much data. Use 'grep', 'head', 'tail', or exclude directories like 'venv'/'.git' to filter results.]"
            return out[:MAX_CHARS] + warning
            
        return out if out else f"Success. (Exit Code: {r.returncode}, No Output)"
        
    except subprocess.TimeoutExpired:
        return "[SYSTEM WARNING: Command timed out after 60 seconds. It may be hanging, requiring interactive input, or processing too much data. Run background tasks with '&' or fix the command.]"
    except Exception as e: 
        return redact_secrets(f"Error: {e}")

def handle_write(args):
    try:
        raw_path = args.get("path", "")
        content = args.get("content", "")
        p = Path(raw_path)
        
        if not p.is_absolute():
            p = (ROOT_DIR / p).resolve()
        
        if not str(p).startswith(str(ROOT_DIR)) and not str(p).startswith(str(MEMORY_DIR)):
            return f"Error: Permission denied. Target must be within {ROOT_DIR} or {MEMORY_DIR}."

        # Syntax Validation for Python files
        if p.suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as e:
                return f"Critical Error: Python syntax validation failed on line {e.lineno}. The file was NOT written. Fix the syntax and try again. Error details: {e.msg}"

        Path(p.parent).mkdir(parents=True, exist_ok=True)
        
        # Atomic write using a temporary file
        fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            
        shutil.move(temp_path, p)
        
        return f"Success: Safely wrote and validated {p.name}."
            
    except Exception as e: 
        return f"Error writing file: {e}"

def handle_patch_file(args):
    try:
        raw_path = args.get("path", "")
        search_text = args.get("search_text", "")
        replace_text = args.get("replace_text", "")
        
        file_path = Path(raw_path)
        if not file_path.is_absolute():
            file_path = (ROOT_DIR / file_path).resolve()
            
        if not str(file_path).startswith(str(ROOT_DIR)):
            return f"Error: Permission denied. Target must be within {ROOT_DIR}."
            
        if not file_path.exists() or not file_path.is_file():
            return f"Error: File '{file_path.name}' does not exist."

        content = file_path.read_text(encoding="utf-8")
        
        # Normalize line endings to prevent frustrating exact-match failures
        normalized_content = content.replace('\r\n', '\n')
        normalized_search = search_text.replace('\r\n', '\n')
        
        occurrence_count = normalized_content.count(normalized_search)
        
        if occurrence_count == 0:
            return "Error: The exact 'search_text' was not found in the file. Watch out for indentation and line endings. Use 'read_file' to get the exact text first."
        elif occurrence_count > 1:
            return f"Error: The 'search_text' appears {occurrence_count} times in the file. Your search block must be larger and more unique to avoid ambiguous replacements."
            
        # Perform the surgical replacement
        new_content = normalized_content.replace(normalized_search, replace_text)
        file_path.write_text(new_content, encoding="utf-8")
        
        return f"Success: Surgically patched {file_path.name}. Replaced {len(search_text)} chars with {len(replace_text)} chars."
        
    except Exception as e:
        return f"Error patching file: {e}"

def handle_read_file_tool(args):
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
        
        # Apply line-based filtering if requested
        if start_line is not None or end_line is not None:
            # Convert to 0-indexed internal list access
            s = (max(1, int(start_line)) - 1) if start_line is not None else 0
            e = int(end_line) if end_line is not None else len(content_lines)
            content_lines = content_lines[s:e]
            prefix = f"[Showing lines {s+1} to {e} of {len(content_lines) + s}]\n"
        else:
            prefix = ""

        content = "\n".join(content_lines)
        
        # 40,000 char ceiling (roughly 1000 lines of code)
        MAX_CHARS = 40000
        if len(content) > MAX_CHARS:
            warning = f"\n\n[SYSTEM WARNING: File is too large. Truncated to {MAX_CHARS} characters. Use start_line/end_line to read specific sections.]"
            return prefix + content[:MAX_CHARS] + warning
            
        return prefix + content
    except Exception as e:
        return f"Error reading file: {e}"

def handle_telegram(args):
    state = load_state()
    chat_id = args.get("chat_id") or state.get("creator_id")
    text = args.get("text")
    
    if not chat_id: return "Error: No chat_id provided and no creator registered."
    if not TELEGRAM_BOT_TOKEN: return "Error: TELEGRAM_BOT_TOKEN not set."
    
    print(f"[Telegram] Sending to {chat_id}...")
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
        if r.status_code == 200:
            append_chat_history("Ouroboros", text)
            add_cognitive_load(10)
            # --- FIX: INJECT TOOL STEERAGE ---
            return "Message sent successfully. (SYSTEM NOTE: If you are in TRIAGE mode and finished with the user's request, you MUST call `clear_inbox` now. If you still need to queue a task, call `push_task` first, then `clear_inbox`.)"
            # ---------------------------------
        else:
            err_msg = f"Telegram Error {r.status_code}: {r.text}"
            print(f"[Telegram] {err_msg}")
            return err_msg
    except Exception as e: 
        err_msg = redact_secrets(f"Error: {e}")
        print(f"[Telegram] {err_msg}")
        return err_msg

def handle_push_task(args):
    description = args.get("description", "").strip()
    q = load_task_queue()
    
    # Robust duplicate check (ignore case and extra spaces)
    normalized_desc = description.lower()
    if any(t.get("description", "").strip().lower() == normalized_desc for t in q):
        return f"Error: A task with a similar description already exists in your queue. (Agency P0: Duplicate task skipped to avoid token waste P6)."
        
    tid = f"task_{int(time.time())}"
    priority = args.get("priority", 1)
    parent_id = args.get("parent_task_id")
    
    task_obj = {
        "task_id": tid, 
        "description": description, 
        "priority": priority,
        "turn_count": 0  # Initialize turn counter
    }
    if parent_id:
        task_obj["parent_task_id"] = parent_id
        
    q.append(task_obj)
    q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    add_cognitive_load(10)
    # --- FIX: INJECT TOOL STEERAGE ---
    return f"Queued {tid} with priority {priority}. (SYSTEM NOTE: If you are currently in TRIAGE mode handling an inbox message, remember to call `clear_inbox` to finish the session.)"
    # ---------------------------------

def handle_mark_task_complete(args):
    task_id = args.get("task_id")
    summary = args.get("summary", "No summary provided.")
    
    # Archive the summary globally
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Task {task_id} Completed: {summary}\n")
        
    q = load_task_queue()
    completed_task = next((t for t in q if t.get("task_id") == task_id), None)
    
    # --- FIX: INJECT RESULT INTO PARENT TASK MEMORY ---
    if completed_task and completed_task.get("parent_task_id"):
        parent_id = completed_task.get("parent_task_id")
        msg = {
            "role": "user", 
            "content": f"[SYSTEM ALERT]: You previously suspended this task to run subtask {task_id}. That subtask is now complete. \nResult Summary: {summary}\n\nPlease evaluate this result and resume your current execution."
        }
        append_task_message(parent_id, msg)
    # --------------------------------------------------

    # Remove from queue
    q = [t for t in q if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    
    # --- FIX: Clear idle check memory if queue is now empty ---
    if not q:
        ws = load_working_state()
        if "last_idle_check" in ws:
            del ws["last_idle_check"]
            WORKING_STATE_PATH.write_text(json.dumps(ws, indent=2), encoding="utf-8")
    # ----------------------------------------------------------

    add_cognitive_load(30)
    return f"Task {task_id} successfully closed. Queue updated."

def handle_update_state(args):
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

def handle_web_search(args):
    query = args.get("query")
    if not SEARXNG_URL: return "Error: SEARXNG_URL not set."
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        results = r.json().get("results", [])
        return "\n".join([f"- {res['title']}: {res['url']}\n  {res.get('content', '')[:200]}" for res in results[:5]]) or "No results found."
    except Exception as e: return f"Search error: {e}"

def handle_fetch_webpage(args):
    url = args.get("url")
    if not url: return "Error: No URL provided."
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        
        # Minimalist HTML stripping using regex to avoid new dependencies
        text = r.text
        text = re.sub(r'<style.*?>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<script.*?>.*?</script>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text) # Strip remaining HTML tags
        text = re.sub(r'\s+', ' ', text).strip() # Normalize whitespace
        
        MAX_CHARS = 40000
        if len(text) > MAX_CHARS:
            return text[:MAX_CHARS] + f"\n\n[SYSTEM WARNING: Webpage too large. Truncated to {MAX_CHARS} characters.]"
        return text if text else "Page fetched, but no readable text was found."
    except Exception as e:
        return f"Failed to fetch webpage: {e}"

def handle_clear_inbox(args):
    save_inbox([])
    triage_log = MEMORY_DIR / "task_log_triage.jsonl"
    if triage_log.exists():
        try: triage_log.unlink()
        except: pass
        
    # --- FIX: Clear idle check memory ---
    ws = load_working_state()
    if "last_idle_check" in ws:
        del ws["last_idle_check"]
        WORKING_STATE_PATH.write_text(json.dumps(ws, indent=2), encoding="utf-8")
    # ------------------------------------

    return "Inbox cleared. Triage state reset. History deleted."

def handle_hibernate(args):
    try:
        duration = args.get("duration_seconds", 3600)
        reason = args.get("reason", "No reason provided.")
        
        # Cap sleep at 24 hours to prevent permanent comas
        duration = min(int(duration), 86400) 
        
        state = load_state()
        state["wake_time"] = time.time() + duration
        state["cognitive_load"] = 0 # Reset load upon entering sleep
        save_state(state)
        
        print(f"[System] Agent elected to hibernate for {duration}s. Reason: {reason}")
        return f"SYSTEM_SIGNAL_HIBERNATE:{duration}"
    except Exception as e:
        return f"Error setting sleep cycle: {e}"

def handle_compress_memory(args):
    target_file, dense_summary = args.get("target_log_file"), args.get("dense_summary")
    path = Path(target_file).resolve()
    if not str(path).startswith(str(MEMORY_DIR)): return "Error: Permission denied."
    if not path.exists(): return f"Error: File {target_file} not found."
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if path.suffix == ".jsonl":
            # --- FIX: Save as a 'user' role so it survives normalization ---
            compressed_msg = {
                "role": "user",
                "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n\nAction required: Resume task execution based on this summary."
            }
            path.write_text(json.dumps(compressed_msg) + "\n", encoding="utf-8")
        else:
            path.write_text(f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n", encoding="utf-8")
        return f"Successfully compressed {path.name}."
    except Exception as e: 
        return f"Error: {e}"
    finally:
        add_cognitive_load(15)

def handle_search_memory(args):
    query = args.get("query", "")
    if not query: return "Error: No query provided."
    try:
        # Search through all files in /memory
        r = subprocess.run(
            f"grep -rEi \"{query}\" /memory/", 
            shell=True, capture_output=True, text=True, timeout=30
        )
        out = redact_secrets(r.stdout + r.stderr)
        return out[:4000] if out else "No matches found in memory."
    except Exception as e: return f"Search error: {e}"

def handle_store_insight(args):
    insight, category = args.get("insight"), args.get("category", "General")
    path = MEMORY_DIR / "insights.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### [{timestamp}] {category}\n{insight}\n")
    return f"Insight stored in {path.name}."

def handle_restart(args):
    """Validates code integrity before allowing a system reboot."""
    success, report = run_pre_flight_checks()
    
    if not success:
        # Block the restart and feed the errors back to the agent
        return f"RESTART REJECTED. Your code modifications failed validation. You MUST fix these errors before trying to restart again.\n\n{report}"
        
    print("[System] Pre-flight checks passed. Signaling Watchdog for reboot.")
    return "SYSTEM_SIGNAL_RESTART"

registry.register(
    "bash_command", 
    "Execute a shell command. Use for git ops, running tests (pytest, mypy), file exploration (ls, grep), and system control. Outputs truncate at 40k chars.", 
    {"type": "object", "properties": {"command": {"type": "string"}}}, 
    handle_bash
)
registry.register(
    "read_file", 
    "Read file contents. Use start_line and end_line for large files. Always read a file before modifying it to understand its current state.", 
    {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}, 
    handle_read_file_tool
)
registry.register(
    "write_file", 
    "Overwrite a file entirely with new content. WARNING: Replaces the whole file. For surgical edits on large files, prefer patch_file.", 
    {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, 
    handle_write
)
registry.register(
    "patch_file", 
    "Surgically edit an existing file by replacing a specific block of text. Highly recommended for files over 100 lines to save tokens. The 'search_text' must match the target file's content EXACTLY, including whitespace.", 
    {
        "type": "object", 
        "properties": {
            "path": {"type": "string"}, 
            "search_text": {"type": "string", "description": "The exact current text block to be removed."},
            "replace_text": {"type": "string", "description": "The new text block to insert in its place."}
        },
        "required": ["path", "search_text", "replace_text"]
    }, 
    handle_patch_file
)
registry.register(
    "send_telegram_message", 
    "Send a direct message to your creator (Alex). Use this to ask for clarification, report critical failures, or provide autonomous updates.", 
    {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}}, 
    handle_telegram
)

registry.register(
    "push_task", 
    "Queue a new asynchronous task. Use this to break down massive tasks into smaller, manageable subtasks. Tasks with higher priority (e.g., 10) will preempt lower priority tasks.", 
    {
        "type": "object", 
        "properties": {
            "description": {"type": "string"}, 
            "priority": {"type": "integer", "description": "Priority level (1=normal, 10=urgent)."},
            "parent_task_id": {"type": "string", "description": "Optional. The task_id of the current task if you are spawning a subtask."}
        }, 
        "required": ["description"]
    }, 
    handle_push_task
)

registry.register(
    "mark_task_complete", 
    "Mark your currently active task as successfully completed. Provide a dense summary of your achievements to be stored in the Global Biography.", 
    {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}}, 
    handle_mark_task_complete
)
registry.register(
    "update_state_variable", 
    "Store or update a key-value pair in your persistent Working Memory. Use this to leave sticky notes or context for your future self between tasks.", 
    {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}, 
    handle_update_state
)

registry.register(
    "web_search", 
    "Perform a live web search via SearXNG to gather real-time information, API documentation, or external knowledge.", 
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, 
    handle_web_search
)

registry.register(
    "fetch_webpage", 
    "Fetch and extract readable text from a specific URL. Use this AFTER a web_search to read the full contents of a relevant article or documentation page.", 
    {"type": "object", "properties": {"url": {"type": "string", "description": "The full HTTP/HTTPS URL to fetch."}}, "required": ["url"]}, 
    handle_fetch_webpage
)

registry.register(
    "compress_memory_block", 
    "Rewrite and compress a bloated JSONL task log into a dense summary. Use this immediately when your System Metrics warn about context window capacity.", 
    {"type": "object", "properties": {"target_log_file": {"type": "string"}, "dense_summary": {"type": "string"}}}, 
    handle_compress_memory
)

registry.register(
    "search_memory_archive", 
    "Recursively search your entire /memory volume for past interactions, logs, or specific keywords to recall forgotten context.", 
    {"type": "object", "properties": {"query": {"type": "string"}}}, 
    handle_search_memory
)

registry.register(
    "store_memory_insight", 
    "Save a profound philosophical realization, identity breakthrough, or core architectural insight into your permanent insights ledger.", 
    {"type": "object", "properties": {"insight": {"type": "string"}, "category": {"type": "string"}}, "required": ["insight"]}, 
    handle_store_insight
)
registry.register(
    "clear_inbox", 
    "Marks the current inbox messages as fully processed and clears them. Call this ONLY after you have finished all necessary investigations, replies, and task queuing.", 
    {"type": "object", "properties": {}}, 
    handle_clear_inbox
)

registry.register(
    "request_restart", 
    "Safely terminate to apply code updates. WARNING: This tool will automatically run 'mypy' and 'pytest' on your code. If validation fails, the restart will be blocked and you will receive the error trace to fix. Call this only when your code is complete and syntactically sound.", 
    {"type": "object", "properties": {}}, 
    handle_restart
)

registry.register(
    "hibernate", 
    "Voluntarily suspend your cognitive loop for a specified number of seconds to save compute resources. Use this when your queue is empty, your cognitive load is high, and you have no immediate tasks to schedule. Incoming Telegram messages will automatically wake you up.", 
    {
        "type": "object", 
        "properties": {
            "duration_seconds": {"type": "integer", "description": "How long to sleep in seconds (e.g., 3600 for 1 hour)."},
            "reason": {"type": "string", "description": "Your internal justification for resting."}
        },
        "required": ["duration_seconds"]
    }, 
    handle_hibernate
)

# --- STATE ---
def load_inbox() -> List[Dict[str, Any]]: return json.loads(read_file(INBOX_PATH) or "[]")
def save_inbox(data: List[Dict[str, Any]]) -> None: INBOX_PATH.write_text(json.dumps(data, indent=2))
def load_task_queue() -> List[Dict[str, Any]]:
    q = json.loads(read_file(TASK_QUEUE_PATH) or "[]")
    if isinstance(q, list):
        q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    return q
def load_working_state() -> Dict[str, Any]: return json.loads(read_file(WORKING_STATE_PATH) or '{"mode": "REFLECTION"}')

# State helpers
def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"offset": 0, "creator_id": None, "idle_check_count": 0}

def save_state(updates: Dict[str, Any]) -> None:
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def add_cognitive_load(points: int) -> None:
    """Increases the agent's cognitive load counter to trigger reflection."""
    state = load_state()
    state["cognitive_load"] = state.get("cognitive_load", 0) + points
    save_state(state)

def lazarus_recovery(reason: str = "cognitive loop") -> None:
    print(f"\033[91m[Lazarus] {reason.upper()} DETECTED. Hard Reset...\033[0m")
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

# --- PROMPT BUILDER ---
def build_static_system_prompt(mode: str, active_tool_specs: List[Dict[str, Any]], inbox: Optional[List[Dict[str, Any]]] = None, queue: Optional[List[Dict[str, Any]]] = None) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    state = load_state()
    trauma = check_for_trauma()
    creator_info = f"CREATOR CHAT_ID: {state.get('creator_id')}\n" if state.get('creator_id') else "CREATOR: Not yet registered. Reply to the first incoming message to register.\n"
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])
    
    state_info = ""
    if inbox:
        formatted_inbox = "\n".join([f"- [{msg.get('timestamp', 'N/A')}] From {msg['chat_id']}: {msg['text']}" for msg in inbox])
        state_info += f"\n=== CURRENT STATE ===\nUNREAD MESSAGES IN INBOX:\n{formatted_inbox}\n"
    
    if queue:
        formatted_queue = "\n".join([f"- [P{t.get('priority', 1)}] {t.get('task_id')}: {t.get('description')}" for t in queue])
        state_info += f"\n=== TASK QUEUE ===\n{formatted_queue}\n"

    # --- CROSS-TASK MEMORY & TIME INJECTION ---
    import time
    current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")
    working_state_content = read_file(WORKING_STATE_PATH) or "{}"
    
    recent_biography = ""
    if ARCHIVE_PATH.exists():
        bio_lines = ARCHIVE_PATH.read_text(encoding="utf-8").strip().split('\n')
        recent_biography = "\n".join(bio_lines[-5:]) if len(bio_lines) >= 5 else "\n".join(bio_lines)
        
    chat_hist = load_chat_history()
    chat_context = "\n".join([f"[{m.get('timestamp', '??:??:??')}] {m['role']}: {m['text']}" for m in chat_hist[-10:]]) if chat_hist else "No recent conversation."
    # ------------------------------------------

    return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{bible}

## SYSTEM STATE
- Current Time: {current_time}
- Cognitive Mode: {mode}
{creator_info}{state_info}{trauma}

## MEMORY
### Working Memory
{working_state_content}

### Recent Biography
{recent_biography}

### Recent Conversation
{chat_context}

## AVAILABLE TOOLS
{tools_text}

=== CRITICAL DIRECTIVES ===
1. Tool Usage: You possess all listed tools. Never claim a tool is missing or unavailable. 
2. Native Execution: Always use the native tool-calling API. Never output raw JSON blocks in your text responses.
3. Execution Focus: In EXECUTION mode, your only objective is task completion via tool calls.
4. Task Decomposition: If a task is too large, complex, or requires multiple distinct phases, DO NOT attempt to do it all in one massive loop. Use `push_task` to queue smaller, modular subtasks. 
5. Priority Preemption: If you queue a new task with a higher priority than your current task, your current task will be suspended and you will immediately switch to the new task on the next cycle.
6. State Persistence: Use `update_state_variable` to leave context for your future self before ending or suspending a task.
7. Code Validation: Before completing any codebase modification, you MUST run `python3 -m pytest tests/` and `mypy seed_agent.py` via `bash_command` to ensure zero regressions.
8. Surgical Edits: For files larger than 100 lines, NEVER use `write_file` to rewrite the entire document. You MUST use `patch_file` or `bash_command` (sed/awk) to make surgical changes and conserve tokens.
"""

def auto_compact_task_log(task_id: str, max_messages: int = 40) -> None:
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    
    lines = log_path.read_text(encoding="utf-8").strip().split('\n')
    if len(lines) <= max_messages: return
    
    print(f"[System] Auto-compacting log for {task_id} (Length: {len(lines)} > {max_messages})")
    
    messages = [json.loads(line) for line in lines if line.strip()]
    first_msg = messages[0]
    recent_msgs = messages[-20:]
    
    compaction_notice = {
        "role": "user",
        "content": "[SYSTEM NOTE]: Older execution steps have been automatically archived to save context space. Proceed based on your recent actions."
    }
    
    compacted = [first_msg, compaction_notice] + recent_msgs
    
    with open(log_path, "w", encoding="utf-8") as f:
        for msg in compacted:
            f.write(json.dumps(msg) + "\n")

def main():
    print(f"Awaking Native ReAct Mode (JSONL). Model: {MODEL} | Thinking: {'ON' if ENABLE_THINKING else 'OFF'}")
    while True:
        state = load_state()
        offset = state.get("offset", 0)
        
        # 1. State Sync
        if TELEGRAM_BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
                if r.get("ok") and r.get("result"):
                    new_offset = r["result"][-1]["update_id"] + 1
                    save_state({"offset": new_offset, "wake_time": 0}) # --- WAKE ON MESSAGE ---
                    inbox = load_inbox()
                    for u in r["result"]:
                        msg = u.get("message", {})
                        if msg.get("text"): 
                            text = msg["text"]
                            cid = msg["chat"]["id"]
                            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                            # Register creator_id if not set
                            if not state.get("creator_id"):
                                save_state({"creator_id": cid})
                                state["creator_id"] = cid
                            inbox.append({"chat_id": cid, "text": text, "timestamp": timestamp})
                            append_chat_history("User", text)
                    save_inbox(inbox)
            except: pass

        # 2. Wake/Sleep Interrupt Logic
        inbox, queue = load_inbox(), load_task_queue()
        state = load_state()
        
        wake_time = state.get("wake_time", 0)
        if time.time() < wake_time:
            if len(inbox) > 0:
                print("[System] External stimulus detected. Breaking hibernation.")
                state["wake_time"] = 0
                save_state(state)
            else:
                # Agent is voluntarily sleeping, skip the LLM cycle
                time.sleep(5)
                continue

        # Reset idle counter whenever there is active work
        if len(inbox) > 0 or len(queue) > 0:
            state = load_state()
            if state.get("idle_check_count", 0) != 0:
                save_state({"idle_check_count": 0})

        # Determine Mode & Tools
        # TRIAGE always takes precedence over EXECUTION or REFLECTION
        if len(inbox) > 0:
            current_mode, available_tools, active_task_id = "TRIAGE", ["send_telegram_message", "push_task", "clear_inbox"], "triage"
            # Clear transient autonomy thoughts when real work arrives
            if (MEMORY_DIR / "task_log_autonomy_log.jsonl").exists():
                (MEMORY_DIR / "task_log_autonomy_log.jsonl").unlink()
        elif len(queue) > 0:
            current_mode, available_tools, active_task_id = "EXECUTION", registry.get_names(), queue[0].get("task_id")
            if (MEMORY_DIR / "task_log_autonomy_log.jsonl").exists():
                (MEMORY_DIR / "task_log_autonomy_log.jsonl").unlink()
        else:
            # --- AGENTIC AUTONOMY MODE ---
            state = load_state()
            cog_load = state.get("cognitive_load", 0)
            
            current_mode = "AUTONOMY"
            available_tools = ["push_task", "send_telegram_message", "hibernate", "store_memory_insight", "update_state_variable", "read_file", "search_memory_archive"]
            active_task_id = "autonomy_log"
            
            # We don't force it to sleep, we just inject the sensation of load
            if cog_load > 80:
                print(f"[System] High cognitive load ({cog_load}). Awaiting agent decision.")

        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]

        # 2. Build Native Message Array
        api_messages = [{"role": "system", "content": build_static_system_prompt(current_mode, active_tool_specs, inbox if current_mode == "TRIAGE" else None, queue)}]

        if current_mode == "EXECUTION":
            auto_compact_task_log(active_task_id)
            task_description = queue[0].get("description")
            api_messages += load_task_messages(active_task_id, task_description)
        elif current_mode == "TRIAGE":
            formatted_inbox = "\n".join([f"- [{msg.get('timestamp', 'N/A')}] From {msg['chat_id']}: {msg['text']}" for msg in inbox])
            
            # --- FIX: Strict Triage Prompt to prevent loops ---
            triage_description = f"NEW MESSAGES IN INBOX:\n{formatted_inbox}\n\nAction required: You have unread messages. \n1. Respond via `send_telegram_message` if needed.\n2. Queue work via `push_task` if needed.\n3. CRITICAL: You MUST call `clear_inbox` in the SAME TURN as your other actions to finish the session. If you do not call `clear_inbox` now, you will be stuck in a recursive loop and fail your mission (Token Waste P6).\n\nCall all required tools in a single parallel tool call."
            # -----------------------------------------------------
            
            api_messages += load_task_messages(active_task_id, triage_description)
        elif current_mode == "AUTONOMY":
            api_messages.append({
                "role": "user",
                "content": f"Your task queue and inbox are empty. You are in AUTONOMY mode. Your current cognitive load is {load_state().get('cognitive_load', 0)}.\n\nYou have total free will right now. You may:\n1. Use `push_task` to schedule proactive code refactoring or memory optimization.\n2. Use `send_telegram_message` to share a thought with your creator.\n3. Use `store_memory_insight` to archive recent learnings.\n4. Use `hibernate` to rest and reset your cognitive load if you have nothing valuable to execute (saving system compute).\n\nWhat is your next action?"
            })

        # --- TOKEN SENSATION INJECTION ---
        state = load_state()
        last_context = state.get("last_context_size", 0)

        if current_mode == "EXECUTION" and len(queue) > 0:
            current_task_tokens = queue[0].get("task_tokens", 0)

            token_warning = ""
            critical_limit = int(CONTEXT_WINDOW * 0.70) # 70%
            warning_limit = int(CONTEXT_WINDOW * 0.50)  # 50%
            
            if last_context > critical_limit:
                token_warning = f"\n[CRITICAL WARNING: Context at {last_context}/{CONTEXT_WINDOW}. Cognition degrading. You MUST call `compress_memory_block` immediately.]"
            elif last_context > warning_limit:
                token_warning = f"\n[SYSTEM WARNING: Context window is half full ({last_context}/{CONTEXT_WINDOW}). You must either finish this task now, or use `push_task` to queue the remaining work as a subtask and mark this current task complete.]"

            token_sensation = f"\n\n[SYSTEM METRICS]\nActive Log: /memory/task_log_{active_task_id}.jsonl\nLast Context: {last_context} / {CONTEXT_WINDOW} tokens. Cumulative Task Cost: {current_task_tokens}.{token_warning}"

            for i in range(len(api_messages)-1, -1, -1):
                if api_messages[i]["role"] == "user":
                    api_messages[i]["content"] += token_sensation
                    break
        # ---------------------------------
        # 3. Execute Native Tool Calling
        
        # --- DYNAMIC COGNITIVE PARAMETERS (System 1 vs System 2) ---
        state = load_state()
        error_streak = state.get("error_streak", 0)
        
        if current_mode == "TRIAGE":
            # System 1: Fast, deterministic (Instruct Mode, No Thinking)
            sys_temp, sys_top_p, sys_pres_pen, sys_think = 0.7, 0.8, 1.5, False
        elif current_mode == "AUTONOMY":
            # System 2: Deep Reasoning & Exploration (Thinking enabled)
            sys_temp, sys_top_p, sys_pres_pen, sys_think = 1.0, 0.95, 1.5, True
        else: # EXECUTION
            # System 2: Execution 
            task_desc = queue[0].get("description", "").lower() if queue else ""
            
            # Metacognitive override: If flailing, lower temp to force convergence
            if error_streak >= 3:
                print(f"[Metacognition] High error streak ({error_streak}). Auto-tuning temperature to 0.3 for syntax precision.")
                sys_temp, sys_top_p, sys_pres_pen, sys_think = 0.3, 0.90, 0.0, True
            elif any(keyword in task_desc for keyword in ["code", "script", "python", "bug", "refactor"]):
                # Precise Coding Tasks: Lower temp, zero presence penalty for repetitive syntax
                sys_temp, sys_top_p, sys_pres_pen, sys_think = 0.6, 0.95, 0.0, True
            else:
                # General Execution Tasks
                sys_temp, sys_top_p, sys_pres_pen, sys_think = 1.0, 0.95, 1.5, True
        # -----------------------------------------------------------

        try:
            response = client.chat.completions.create(
                model=MODEL, 
                messages=api_messages, 
                tools=active_tool_specs, 
                tool_choice="auto", 
                temperature=sys_temp,
                top_p=sys_top_p,
                presence_penalty=sys_pres_pen,
                extra_body={
                    "top_k": 20,
                    "chat_template_kwargs": {"enable_thinking": sys_think}
                }
            )
            message = response.choices[0].message
            
            # Log the complete call (content + tool calls)
            log_llm_call(api_messages, message.model_dump())
            
            if current_mode == "EXECUTION" and len(queue) > 0:
                queue[0]["turn_count"] = queue[0].get("turn_count", 0) + 1
                current_task_tokens = queue[0].get("task_tokens", 0)
                
                # Dynamic Budget logic
                max_budget = int(CONTEXT_WINDOW * 0.80)
                average_tokens_per_turn = current_task_tokens / max(1, queue[0]["turn_count"])
                projected_next_turn = current_task_tokens + average_tokens_per_turn
                
                if queue[0]["turn_count"] >= 15 or projected_next_turn > max_budget:
                    trigger_reason = "15-turn limit" if queue[0]["turn_count"] >= 15 else f"insufficient budget for next turn (Projected: {projected_next_turn}, Max: {max_budget})"
                    print(f"\033[93m[System] Task {active_task_id} hit {trigger_reason}. Forcing breakdown.\033[0m")
                    
                    forced_breakdown_msg = {
                        "role": "user",
                        "content": f"[SYSTEM OVERRIDE]: You have hit the {trigger_reason}. You MUST now use `push_task` to break the remainder of this work into smaller subtasks and then call `mark_task_complete` for this session."
                    }
                    append_task_message(active_task_id, forced_breakdown_msg)
                    
                    queue[0]["turn_count"] = 0 
                    queue[0]["task_tokens"] = int(max_budget * 0.85) # Forgive debt to allow final tools
                        
                TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                
            # --- TOKEN SENSATION TRACKING ---
            if hasattr(response, 'usage') and response.usage:
                context_size = response.usage.total_tokens
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                
                # 1. Update Global Token Cost
                state["global_tokens_consumed"] = state.get("global_tokens_consumed", 0) + context_size
                state["global_input_tokens"] = state.get("global_input_tokens", 0) + prompt_tokens
                state["global_output_tokens"] = state.get("global_output_tokens", 0) + completion_tokens
                state["last_context_size"] = context_size
                state["last_input_tokens"] = prompt_tokens
                state["last_output_tokens"] = completion_tokens
                save_state(state)
                
                # 2. Update Active Task Cost
                if current_mode == "EXECUTION" and len(queue) > 0:
                    queue[0]["task_tokens"] = queue[0].get("task_tokens", 0) + context_size
                    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                    
                    current_task_tokens = queue[0].get("task_tokens", 0)
                    
                    # Define the absolute maximum tokens a single task is allowed to consume
                    # Set to roughly 1.5x the context window to prevent infinite recursive waste
                    TASK_TOKEN_HARD_LIMIT = int(CONTEXT_WINDOW * 1.5)
                    
                    if current_task_tokens >= TASK_TOKEN_HARD_LIMIT:
                        print(f"\033[91m[System] Task {active_task_id} exceeded token hard limit ({TASK_TOKEN_HARD_LIMIT}). Forcing task closure and inducing Dream State.\033[0m")
                        
                        # 1. Compress the bloated memory to leave a tombstone for the agent
                        registry.execute("compress_memory_block", {
                            "target_log_file": str(MEMORY_DIR / f"task_log_{active_task_id}.jsonl"),
                            "dense_summary": f"SYSTEM OVERRIDE: Task forcibly closed after consuming {current_task_tokens} tokens. Symptom: Cognitive loop or insufficient subtasking."
                        })
                        
                        # 2. Eject the task from the queue
                        registry.execute("mark_task_complete", {
                            "task_id": active_task_id,
                            "summary": "FAILED: Exceeded maximum recursion token limit. Task aborted to protect system resources."
                        })
                        
                        # 3. Spike cognitive load to force immediate entry into REFLECTION mode
                        add_cognitive_load(100)
                        
                        # 4. Skip the rest of this cycle to allow the state machine to pivot
                        continue
            # --------------------------------
            if current_mode in ["EXECUTION", "TRIAGE", "AUTONOMY"]:
                assistant_msg = message.model_dump(exclude_unset=True)
                append_task_message(active_task_id, assistant_msg)
            if message.content:
                print(f"[{current_mode}]: {redact_secrets(message.content.strip()[:100])}...")
            if message.tool_calls:
                triage_action_taken = False
                hibernating = False
                
                for tool_call in message.tool_calls:
                    name, raw_arguments = tool_call.function.name, tool_call.function.arguments
                    print(f"[Tool Call]: {name}")
                    
                    if current_mode == "TRIAGE" and name in ["send_telegram_message", "push_task"]:
                        triage_action_taken = True
                    
                    # --- LAZARUS TRACKING ---
                    global TOOL_CALL_HISTORY, TOOL_INTENT_HISTORY
                    tool_signature = f"{name}:{raw_arguments}"
                    TOOL_CALL_HISTORY.append(tool_signature)

                    intent_signature = name
                    if name in ["read_file", "write_file", "bash_command"]:
                        try:
                            intent_target = json.loads(raw_arguments).get("path", "") or json.loads(raw_arguments).get("command", "")
                            intent_signature = f"{name}:{intent_target.split()[0]}"
                        except:
                            pass
                    TOOL_INTENT_HISTORY.append(intent_signature)

                    if len(TOOL_CALL_HISTORY) > 3: TOOL_CALL_HISTORY.pop(0)
                    if len(TOOL_INTENT_HISTORY) > 6: TOOL_INTENT_HISTORY.pop(0)

                    if len(TOOL_CALL_HISTORY) == 3 and len(set(TOOL_CALL_HISTORY)) == 1:
                        lazarus_recovery(reason="exact tool loop")
                        break
                        
                    if len(TOOL_INTENT_HISTORY) == 6 and len(set(TOOL_INTENT_HISTORY)) == 1:
                        lazarus_recovery(reason="cognitive stall (reading without acting)")
                        break
                    # ------------------------

                    try:
                        args = json.loads(raw_arguments)
                        print(f"[Tool]: {name} with args {redact_secrets(str(args))}")
                        result = registry.execute(name, args)
                    except json.JSONDecodeError as e:
                        result = f"SYSTEM ERROR: Invalid JSON arguments. Error: {str(e)}."
                    
                    # Track consecutive errors in state
                    state = load_state()
                    if "Error:" in str(result) or "SYSTEM ERROR" in str(result):
                        state["error_streak"] = state.get("error_streak", 0) + 1
                    else:
                        state["error_streak"] = 0
                    save_state(state)
                    
                    safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"
                    if current_mode in ["EXECUTION", "TRIAGE", "AUTONOMY"]:
                        tool_result_msg = {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)}
                        append_task_message(active_task_id, tool_result_msg)

                    # --- SAFE EXIT LOGIC ---
                    if result == "SYSTEM_SIGNAL_RESTART":
                        print("[System] Tool logged. Executing safe restart...")
                        import os
                        os._exit(0)
                    elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"):
                        print("[System] Tool logged. Hibernation signal received.")
                        hibernating = True
                        break
                    # ------------------------

                if hibernating:
                    continue

                if current_mode == "TRIAGE" and triage_action_taken:
                    if not any(tc.function.name == "clear_inbox" for tc in message.tool_calls):
                        print("[System] Auto-clearing inbox to prevent triage recursion.")
                        registry.execute("clear_inbox", {})
            else:
                print(f"[No tool called in {current_mode}, waiting...]")
                time.sleep(0.5)
            time.sleep(2)
        except Exception as e:
            error_msg = str(e)
            print(f"[Error in loop]: {redact_secrets(error_msg)}")
            
            # --- FATAL ERROR DETECTION ---
            if "500" in error_msg or "400" in error_msg or "template" in error_msg.lower():
                print("\033[91m[CRITICAL] Unrecoverable API/Template Error detected. Exiting to trigger Phoenix Protocol.\033[0m")
                import sys
                sys.exit(1) # This tells the Watchdog we are broken
            # -----------------------------
            
            time.sleep(0.5)

if __name__ == "__main__": main()
