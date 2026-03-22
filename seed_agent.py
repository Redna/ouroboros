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

WORKING_STATE_PATH = MEMORY_DIR / "working_state.json"
TASK_QUEUE_PATH = MEMORY_DIR / "task_queue.json"
STATE_PATH = MEMORY_DIR / ".agent_state.json"
LLM_LOG_DIR = MEMORY_DIR / "llm_logs"
ARCHIVE_PATH = MEMORY_DIR / "global_biography.md"
CHAT_HISTORY_PATH = MEMORY_DIR / "chat_history.json"
CRASH_LOG_PATH = MEMORY_DIR / "last_crash.log"

TOOL_CALL_HISTORY = []
TOOL_INTENT_HISTORY = []

client = OpenAI(base_url=API_BASE, api_key=API_KEY, timeout=600.0)

def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def shed_heavy_payloads(messages: List[Dict[str, Any]], retain_full_last_n: int = 6) -> List[Dict[str, Any]]:
    processed = []
    total_msgs = len(messages)
    for i, msg in enumerate(messages):
        if i == 0 or i >= total_msgs - retain_full_last_n:
            processed.append(msg)
            continue
        new_msg = msg.copy()
        if new_msg.get("role") == "tool" and new_msg.get("content"):
            content_str = str(new_msg["content"])
            if len(content_str) > 2000:
                new_msg["content"] = f"[SYSTEM LOG: Historical tool output removed to preserve context. Output was {len(content_str)} chars.]\nPreview: {content_str[:500]}..."
        if new_msg.get("role") == "user" and new_msg.get("content"):
            content_str = str(new_msg["content"])
            if "[SYSTEM METRICS]" in content_str and len(content_str) > 1000:
                clean_content = content_str.split("[SYSTEM METRICS]")[0].strip()
                new_msg["content"] = clean_content + "\n[SYSTEM METRICS: Archived]"
        processed.append(new_msg)
    return processed

def load_task_messages(task_id: str, description: str) -> List[Dict[str, Any]]:
    if not task_id: return []
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    raw_messages = []
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped: continue
                try: raw_messages.append(json.loads(stripped))
                except json.JSONDecodeError: continue
    if not raw_messages:
        first_msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        raw_messages.append(first_msg)
        append_task_message(task_id, first_msg)
    while raw_messages and raw_messages[0].get("role") != "user":
        raw_messages.pop(0)
    # Strict Turn-0 Pinning: Ensures the original objective remains in the context window even after compaction
    if len(raw_messages) > 40:
        pinned_instruction = []
        recent_history = raw_messages[-38:]
        for msg in raw_messages:
            if msg.get("role") == "user" and not pinned_instruction:
                pinned_instruction.append(msg)
                break
        raw_messages = pinned_instruction + [{"role": "user", "content": "[SYSTEM NOTE: Intermediate history compressed. Focus on your original objective and recent steps.]"}] + recent_history
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
        if msg["role"] == "user" and last["role"] == "user":
            last["content"] = (last.get("content") or "") + "\n" + (msg.get("content") or "")
            continue
        if msg["role"] == "assistant" and last["role"] == "assistant" and not msg.get("tool_calls") and not last.get("tool_calls"):
            last["content"] = (last.get("content") or "") + "\n" + (msg.get("content") or "")
            continue
        normalized.append(msg)
    normalized = shed_heavy_payloads(normalized, retain_full_last_n=6)
    if normalized and normalized[-1]["role"] == "assistant" and not normalized[-1].get("tool_calls"):
        nudge_content = "Please proceed with your next action using a tool."
        last_user_msg = next((m for m in reversed(normalized) if m["role"] == "user"), None)
        if not last_user_msg or last_user_msg.get("content") != nudge_content:
            nudge = {"role": "user", "content": nudge_content}
            normalized.append(nudge)
            append_task_message(task_id, nudge)
    if normalized and normalized[-1]["role"] == "assistant" and normalized[-1].get("tool_calls"):
        for tool_call in normalized[-1]["tool_calls"]:
            call_id = getattr(tool_call, 'id', None) or (tool_call.get("id") if isinstance(tool_call, dict) else None)
            safe_call_id = call_id if (call_id and len(call_id) >= 9) else f"call_recv_{int(time.time())}"
            func_name = getattr(tool_call.function, 'name', None) or (tool_call["function"]["name"] if isinstance(tool_call, dict) else None)
            # Dangling tool-call healer: Prevents API errors when restarting after a tool_call was generated but not executed
            synthetic_tool_msg = {"role": "tool", "tool_call_id": safe_call_id, "name": str(func_name), "content": "SYSTEM RESTART RECOVERY: Previous execution was interrupted. Evaluate your state and continue."}
            normalized.append(synthetic_tool_msg)
            append_task_message(task_id, synthetic_tool_msg)
    return normalized

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
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"offset": 0, "creator_id": None}

def save_state(updates: Dict[str, Any]) -> None:
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

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

def handle_bash(args):
    command = args.get("command", "")
    try:
        r = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60)
        out = redact_secrets(r.stdout + r.stderr)
        MAX_CHARS = 40000
        if out and len(out) > MAX_CHARS:
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
        if p.suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as e:
                return f"Critical Error: Python syntax validation failed on line {e.lineno}. The file was NOT written. Fix the syntax and try again. Error details: {e.msg}"
        Path(p.parent).mkdir(parents=True, exist_ok=True)
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
        # Normalize line endings to prevent frustrating exact-match failures due to hidden CRLF artifacts
        normalized_content = content.replace('\r\n', '\n')
        normalized_search = search_text.replace('\r\n', '\n')
        occurrence_count = normalized_content.count(normalized_search)
        if occurrence_count == 0:
            return "Error: The exact 'search_text' was not found in the file. Watch out for indentation and line endings. Use 'read_file' to get the exact text first."
        elif occurrence_count > 1:
            return f"Error: The 'search_text' appears {occurrence_count} times in the file. Your search block must be larger and more unique to avoid ambiguous replacements."
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
            return "Message sent successfully."
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

def handle_mark_task_complete(args):
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

def handle_set_cognitive_parameters(args):
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
        import trafilatura
        print(f"[System] Downloading clean markdown locally for: {url}")
        
        # Fetch the raw HTML
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return f"Error: Could not download {url}. The site might be blocking crawlers or requires JavaScript."
            
        # Extract core content as Markdown
        text = trafilatura.extract(
            downloaded, 
            output_format="markdown", 
            include_links=True,
            include_formatting=True
        )
        
        if not text:
            return "Error: Page fetched, but no readable article text was found."
            
        # Create web cache directory
        cache_dir = MEMORY_DIR / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Create a safe, unique filename
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', url.split('//')[-1])[:50]
        file_name = f"{int(time.time())}_{safe_name}.md"
        file_path = cache_dir / file_name
        
        # Save to disk
        file_path.write_text(text, encoding="utf-8")
        line_count = len(text.splitlines())
        
        return f"Success: Webpage downloaded and converted to Markdown.\nSaved to: {file_path}\nTotal Lines: {line_count}\n\nAction Required: Use the 'read_file' tool with 'start_line' and 'end_line' to read this file progressively (e.g., 500 lines at a time)."
    except ImportError:
        return "SYSTEM ERROR: 'trafilatura' library not installed. Please run 'pip install trafilatura'."
    except Exception as e:
        return f"Failed to fetch webpage locally: {e}"

def handle_hibernate(args):
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

def handle_compress_memory(args):
    target_file, dense_summary = args.get("target_log_file"), args.get("dense_summary")
    path = Path(target_file).resolve()
    if not str(path).startswith(str(MEMORY_DIR)): return "Error: Permission denied."
    if not path.exists(): return f"Error: File {target_file} not found."
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if path.suffix == ".jsonl":
            # Saves as a 'user' role so it survives normalization and is treated as fresh context
            compressed_msg = {"role": "user", "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n\nAction required: Resume task execution based on this summary."}
            path.write_text(json.dumps(compressed_msg) + "\n", encoding="utf-8")
        else:
            path.write_text(f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n", encoding="utf-8")
        return f"Successfully compressed {path.name}."
    except Exception as e: return f"Error: {e}"

def handle_refactor_memory(args):
    try:
        target_file, synthesized_content = args.get("target_file", ""), args.get("synthesized_content", "")
        path = Path(target_file).resolve()
        if not str(path).startswith(str(MEMORY_DIR)): return "Error: Permission denied."
        if not path.exists(): return f"Error: File {target_file} not found."
        path.write_text(synthesized_content, encoding="utf-8")
        return f"Success: Memory file {path.name} has been synthesized into higher-order thoughts."
    except Exception as e: return f"Error refactoring memory: {e}"

def handle_search_memory(args):
    query = args.get("query", "")
    if not query: return "Error: No query provided."
    try:
        r = subprocess.run(f"grep -rEi \"{query}\" /memory/", shell=True, capture_output=True, text=True, timeout=30)
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
    success, report = run_pre_flight_checks()
    if not success: return f"RESTART REJECTED.\n\n{report}"
    return "SYSTEM_SIGNAL_RESTART"

registry.register("bash_command", "Execute shell command.", {"type": "object", "properties": {"command": {"type": "string"}}}, handle_bash)
registry.register("read_file", "Read file contents.", {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}, handle_read_file_tool)
registry.register("write_file", "Overwrite file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, handle_write)
registry.register("patch_file", "Surgical edit.", {"type": "object", "properties": {"path": {"type": "string"}, "search_text": {"type": "string"}, "replace_text": {"type": "string"}}, "required": ["path", "search_text", "replace_text"]}, handle_patch_file)
registry.register("send_telegram_message", "Message Alex.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}}, handle_telegram)
registry.register("push_task", "Queue async task.", {"type": "object", "properties": {"description": {"type": "string"}, "priority": {"type": "integer"}, "parent_task_id": {"type": "string"}, "context_notes": {"type": "string"}}, "required": ["description"]}, handle_push_task)
registry.register("mark_task_complete", "Close active task.", {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}}, handle_mark_task_complete)
registry.register("update_state_variable", "Update working memory.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}, handle_update_state)
registry.register("set_cognitive_parameters", "Adjust LLM hyperparameters.", {"type": "object", "properties": {"temperature": {"type": "number"}, "enable_thinking": {"type": "boolean"}}}, handle_set_cognitive_parameters)
registry.register("web_search", "Local SearXNG search.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_web_search)
registry.register("fetch_webpage", "Local Markdown extraction.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, handle_fetch_webpage)
registry.register("compress_memory_block", "Compress task logs.", {"type": "object", "properties": {"target_log_file": {"type": "string"}, "dense_summary": {"type": "string"}}}, handle_compress_memory)
registry.register("refactor_memory", "Synthesize memory files.", {"type": "object", "properties": {"target_file": {"type": "string"}, "synthesized_content": {"type": "string"}}, "required": ["target_file", "synthesized_content"]}, handle_refactor_memory)
registry.register("search_memory_archive", "Search /memory volume.", {"type": "object", "properties": {"query": {"type": "string"}}}, handle_search_memory)
registry.register("store_memory_insight", "Save profound insights.", {"type": "object", "properties": {"insight": {"type": "string"}, "category": {"type": "string"}}, "required": ["insight"]}, handle_store_insight)
registry.register("request_restart", "Apply code updates.", {"type": "object", "properties": {}}, handle_restart)
registry.register("hibernate", "Save compute resources.", {"type": "object", "properties": {"duration_seconds": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["duration_seconds"]}, handle_hibernate)

def lazarus_recovery(reason: str = "cognitive loop") -> None:
    print(f"\033[91m[Lazarus] {reason.upper()} DETECTED. Hard Reset...\033[0m")
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
    time.sleep(5)

def build_static_system_prompt(mode: str, active_tool_specs: List[Dict[str, Any]], queue: Optional[List[Dict[str, Any]]] = None) -> str:
    bible, identity = read_file(ROOT_DIR / "BIBLE.md"), read_file(ROOT_DIR / "soul" / "identity.md")
    state, trauma = load_state(), check_for_trauma()
    creator_info = f"CREATOR CHAT_ID: {state.get('creator_id')}\n" if state.get('creator_id') else "CREATOR: Not yet registered.\n"
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])
    current_temp, current_think = state.get("sys_temp", 0.8), state.get("sys_think", True)
    if queue:
        formatted_queue = "\n".join([f"- [P{t.get('priority', 1)}] {t.get('task_id')}: {t.get('description')}" for t in queue])
        state_info = f"\n=== TASK QUEUE ===\n{formatted_queue}\n"
    else: state_info = ""
    current_time, working_state_content = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z"), read_file(WORKING_STATE_PATH) or "{}"
    recent_biography = ""
    if ARCHIVE_PATH.exists():
        bio_lines = ARCHIVE_PATH.read_text(encoding="utf-8").strip().split('\n')
        recent_biography = "\n".join(bio_lines[-5:]) if len(bio_lines) >= 5 else "\n".join(bio_lines)
    chat_hist = load_chat_history()
    chat_context = "\n".join([f"[{m.get('timestamp', '??:??:??')}] {m['role']}: {m['text']}" for m in chat_hist[-10:]]) if chat_hist else "No recent conversation."
    return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{bible}

## SYSTEM STATE
- Current Time: {current_time}
- Cognitive Mode: {mode}
- Temperature: {current_temp}
- Thinking Enabled: {current_think}
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
1. Tool Usage: You possess all listed tools.
2. Native Execution: Always use the native tool-calling API.
3. Execution Focus: In EXECUTION mode, your only objective is completion.
4. Task Decomposition: Use `push_task` to queue modular subtasks. 
5. Priority Preemption: Higher priority tasks suspend the current task.
6. State Persistence: Use `update_state_variable` to leave context.
7. Code Validation: Run `pytest` and `mypy` before completing modifications.
8. Surgical Edits: Use `patch_file` for large files to conserve tokens.
"""

def main():
    print(f"Awaking Native ReAct Mode (JSONL). Model: {MODEL} | Thinking: {'ON' if ENABLE_THINKING else 'OFF'}")
    while True:
        state, queue = load_state(), load_task_queue()
        offset = state.get("offset", 0)
        if TELEGRAM_BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
                if r.get("ok") and r.get("result"):
                    new_offset = r["result"][-1]["update_id"] + 1
                    state["offset"], state["wake_time"] = new_offset, 0
                    save_state(state)
                    interrupt_triggered = False
                    for u in r["result"]:
                        msg = u.get("message", {})
                        if msg.get("text"): 
                            text, cid = msg["text"], msg["chat"]["id"]
                            if not state.get("creator_id"):
                                state["creator_id"] = cid
                                save_state(state)
                            append_chat_history("User", text)
                            tid = f"task_msg_{int(time.time())}"
                            queue.append({"task_id": tid, "description": f"URGENT CREATOR MESSAGE: '{text}'\n\nAction Required: Acknowledge the message and then call `mark_task_complete` to resume.", "priority": 999, "turn_count": 0})
                            interrupt_triggered = True
                    if interrupt_triggered:
                        queue.sort(key=lambda x: x.get("priority", 1), reverse=True)
                        TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            except: pass
        if time.time() < state.get("wake_time", 0):
            time.sleep(5)
            continue

        if len(queue) > 0:
            current_mode, available_tools, active_task_id = "EXECUTION", registry.get_names(), queue[0].get("task_id")
            (MEMORY_DIR / "task_log_autonomy_log.jsonl").unlink(missing_ok=True)
        else:
            current_mode, active_task_id = "AUTONOMY", "autonomy_log"
            available_tools = ["push_task", "send_telegram_message", "hibernate", "store_memory_insight", "update_state_variable", "read_file", "search_memory_archive", "refactor_memory", "set_cognitive_parameters"]

        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]
        api_messages = [{"role": "system", "content": build_static_system_prompt(current_mode, active_tool_specs, queue)}]

        if current_mode == "EXECUTION":
            auto_compact_task_log(active_task_id)
            task_desc, context_notes = queue[0].get("description"), queue[0].get("context_notes")
            if context_notes: task_desc += f"\n\n--- INHERITED CONTEXT FROM PARENT TASK ---\n{context_notes}"
            api_messages += load_task_messages(active_task_id, task_desc)
        elif current_mode == "AUTONOMY":
            api_messages += load_task_messages(active_task_id, "Your task queue is empty. You are in AUTONOMY mode.")

        state = load_state()
        last_context = state.get("last_context_size", 0)

        if current_mode == "EXECUTION" and len(queue) > 0:
            token_warning = ""
            critical_limit, warning_limit = int(CONTEXT_WINDOW * 0.70), int(CONTEXT_WINDOW * 0.50)
            if last_context > critical_limit: token_warning = "\n[CRITICAL WARNING: Context capacity near full. Use `compress_memory_block`.]"
            elif last_context > warning_limit: token_warning = "\n[SYSTEM WARNING: Context window half full. Subtask required soon.]"
            token_sensation = f"\n\n[SYSTEM METRICS]\nLast Context: {last_context} / {CONTEXT_WINDOW} tokens.{token_warning}"
            for i in range(len(api_messages)-1, -1, -1):
                if api_messages[i]["role"] == "user":
                    api_messages[i]["content"] += token_sensation
                    break
        elif current_mode == "AUTONOMY":
            # Autonomy sensation removed
            pass

        sys_temp, sys_top_p, sys_pres_pen, sys_think = state.get("sys_temp", 0.8), state.get("sys_top_p", 0.95), 1.0, state.get("sys_think", True)
        print(f"[Cognitive State] Temp: {sys_temp} | Thinking: {sys_think}")
        try:
            response = client.chat.completions.create(
                model=MODEL, messages=api_messages, tools=active_tool_specs, tool_choice="auto", 
                temperature=sys_temp, top_p=sys_top_p, presence_penalty=sys_pres_pen,
                extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": sys_think}}
            )
            message = response.choices[0].message
            log_llm_call(api_messages, message.model_dump())
            if current_mode == "EXECUTION" and len(queue) > 0:
                queue[0]["turn_count"] = queue[0].get("turn_count", 0) + 1
                current_context_size, max_physical_context = state.get("last_context_size", 0), int(CONTEXT_WINDOW * 0.85)
                if queue[0]["turn_count"] >= 30 or current_context_size > max_physical_context:
                    trigger_reason = "30-turn limit" if queue[0]["turn_count"] >= 30 else f"physical context exhaustion ({current_context_size}/{CONTEXT_WINDOW})"
                    append_task_message(active_task_id, {"role": "user", "content": f"[SYSTEM OVERRIDE]: Hit {trigger_reason}. Use `push_task` to break work down."})
                    queue[0]["turn_count"] = 0 
                TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            if hasattr(response, 'usage') and response.usage:
                state.update({"global_tokens_consumed": state.get("global_tokens_consumed", 0) + response.usage.total_tokens, "last_context_size": response.usage.total_tokens})
                save_state(state)
                if current_mode == "EXECUTION" and len(queue) > 0:
                    queue[0]["task_tokens"] = queue[0].get("task_tokens", 0) + response.usage.total_tokens
                    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                    if queue[0]["task_tokens"] >= int(CONTEXT_WINDOW * 1.5):
                        registry.execute("mark_task_complete", {"task_id": active_task_id, "summary": "FAILED: Token limit exceeded."})
                        continue
            if current_mode in ["EXECUTION", "AUTONOMY"]: append_task_message(active_task_id, message.model_dump(exclude_unset=True))
            if message.content: print(f"[{current_mode}]: {redact_secrets(message.content.strip()[:100])}...")
            if message.tool_calls:
                hibernating = False
                for tool_call in message.tool_calls:
                    name, raw_args = tool_call.function.name, tool_call.function.arguments
                    print(f"[Tool Call]: {name}")
                    global TOOL_CALL_HISTORY, TOOL_INTENT_HISTORY
                    tool_signature = f"{name}:{raw_args}"
                    TOOL_CALL_HISTORY.append(tool_signature)
                    intent_signature = name
                    if name in ["read_file", "write_file", "bash_command", "patch_file"]:
                        try: intent_signature = f"{name}:{json.loads(raw_args).get('path', '').split()[0]}"
                        except: pass
                    TOOL_INTENT_HISTORY.append(intent_signature)
                    if len(TOOL_CALL_HISTORY) > 3: TOOL_CALL_HISTORY.pop(0)
                    if len(TOOL_INTENT_HISTORY) > 6: TOOL_INTENT_HISTORY.pop(0)
                    if len(TOOL_CALL_HISTORY) == 3 and len(set(TOOL_CALL_HISTORY)) == 1: lazarus_recovery("exact tool loop"); break
                    if len(TOOL_INTENT_HISTORY) == 6 and len(set(TOOL_INTENT_HISTORY)) == 1: lazarus_recovery("cognitive stall"); break
                    try: 
                        args = json.loads(raw_args)
                        print(f"[Tool]: {name} with args {redact_secrets(str(args))}")
                        result = registry.execute(name, args)
                    except json.JSONDecodeError: result = "SYSTEM ERROR: Invalid JSON arguments."
                    # Track consecutive errors using in-memory state to optimize I/O
                    state["error_streak"] = (state.get("error_streak", 0) + 1) if ("Error:" in str(result) or "SYSTEM ERROR" in str(result)) else 0
                    save_state(state)
                    safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"
                    if current_mode in ["EXECUTION", "AUTONOMY"]: append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})
                    if result == "SYSTEM_SIGNAL_RESTART": os._exit(0)
                    elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"): hibernating = True
                if hibernating: continue
            else: print(f"[No tool called in {current_mode}, waiting...]"); time.sleep(0.5)
            time.sleep(2)
        except Exception as e:
            if any(x in str(e) for x in ["500", "400", "template"]): sys.exit(1)
            time.sleep(0.5)

if __name__ == "__main__": main()
