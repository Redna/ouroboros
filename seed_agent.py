import os
import json
import time
import subprocess
import requests
import re
from pathlib import Path
from openai import OpenAI

# Configuration
API_BASE = os.environ.get("VLLM_BASE_URL", "http://llamacpp:8080/v1")
API_KEY = os.environ.get("VLLM_API_KEY", "local-vllm-key")
MODEL = os.environ.get("OUROBOROS_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
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

client = OpenAI(base_url=API_BASE, api_key=API_KEY, timeout=600.0)

# --- UTILS ---
def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def log_llm_call(messages, response_content):
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = LLM_LOG_DIR / f"call-{timestamp}-{int(time.time())}.json"
        log_data = {"timestamp": timestamp, "model": MODEL, "messages": messages, "response": response_content}
        log_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception as e: 
        print(f"[System] LLM Log Error: {e}")

def redact_secrets(text: str) -> str:
    if not text: return text
    if TELEGRAM_BOT_TOKEN: text = text.replace(TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if GITHUB_TOKEN: text = text.replace(GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

def check_for_trauma():
    """Checks for crash logs and returns a warning message if found."""
    if CRASH_LOG_PATH.exists():
        try:
            error_data = CRASH_LOG_PATH.read_text(encoding="utf-8")
            CRASH_LOG_PATH.unlink() # Delete after reading
            return f"\n\n[SYSTEM WARNING: TRAUMA DETECTED]\nMy previous execution crashed. Here are the last logs before the failure:\n---\n{error_data}\n---\nI must analyze this error and avoid repeating the logic that caused it."
        except: pass
    return ""

# --- TASK MESSAGES (JSONL) ---
def load_task_messages(task_id: str, description: str) -> list:
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
    
    # KEEP THIS: Keep the last 60 messages to leverage the full context window safely
    raw_messages = raw_messages[-60:]
    
    # If the slicing removed the starting 'user' message, fix it
    while raw_messages and raw_messages[0].get("role") != "user":
        raw_messages.pop(0)

    if not raw_messages:
        raw_messages = [{"role": "user", "content": f"Resume execution of task: {description}"}]

    normalized = []
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

def append_task_message(task_id: str, message_dict: dict):
    """Appends an OpenAI-compliant message dictionary."""
    if not task_id: return
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")

# --- CHAT HISTORY ---
def load_chat_history():
    if CHAT_HISTORY_PATH.exists():
        try: return json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def append_chat_history(role, text):
    history = load_chat_history()
    history.append({"role": role, "text": text})
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
        
        MAX_CHARS = 24000
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
        p = Path(args.get("path")).resolve()
        Path(p.parent).mkdir(parents=True, exist_ok=True)
        p.write_text(args.get("content", ""), encoding="utf-8")
        return f"Wrote {p.name}."
    except Exception as e: return str(e)

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
        
        # 24,000 char ceiling (roughly 600 lines of code)
        MAX_CHARS = 24000
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
    q = load_task_queue(); tid = f"task_{int(time.time())}"
    q.append({"task_id": tid, "description": args.get("description"), "priority": 1})
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    return f"Queued {tid}."

def handle_clear_inbox(args):
    save_inbox([])
    triage_log = MEMORY_DIR / "task_log_triage.jsonl"
    if triage_log.exists():
        try: triage_log.unlink()
        except: pass
    return "Inbox cleared. Triage state reset. History deleted."

def handle_mark_task_complete(args):
    task_id = args.get("task_id")
    summary = args.get("summary", "No summary provided.")
    
    # Archive the summary
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Task {task_id} Completed: {summary}\n")
        
    # Remove from queue
    q = load_task_queue()
    q = [t for t in q if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
    return f"Task {task_id} successfully closed. Queue updated."

def handle_update_state(args):
    return f"State updated: {args}"

def handle_web_search(args):
    query = args.get("query")
    if not SEARXNG_URL: return "Error: SEARXNG_URL not set."
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        results = r.json().get("results", [])
        return "\n".join([f"- {res['title']}: {res['url']}\n  {res.get('content', '')[:200]}" for res in results[:5]]) or "No results found."
    except Exception as e: return f"Search error: {e}"

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
    except Exception as e: return f"Error: {e}"

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
    """Returns a signal rather than killing the process immediately."""
    return "SYSTEM_SIGNAL_RESTART"

registry.register("bash_command", "Execute bash.", {"type": "object", "properties": {"command": {"type": "string"}}}, handle_bash)
registry.register("read_file", "Read file with line support.", {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}, handle_read_file_tool)
registry.register("write_file", "Write file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, handle_write)
registry.register("send_telegram_message", "Telegram.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}}, handle_telegram)
registry.register("push_task", "Queue task.", {"type": "object", "properties": {"description": {"type": "string"}}}, handle_push_task)
registry.register("clear_inbox", "Marks the current inbox messages as fully processed and clears them. Call this ONLY after you have finished all necessary investigations, replies, and task queuing.", {"type": "object", "properties": {}}, handle_clear_inbox)
registry.register(
    "mark_task_complete", 
    "Close active task.", 
    {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}}, 
    handle_mark_task_complete
)
registry.register("update_state_variable", "Update state.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}, handle_update_state)
registry.register("web_search", "Search web.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_web_search)
registry.register("compress_memory_block", "Compress log.", {"type": "object", "properties": {"target_log_file": {"type": "string"}, "dense_summary": {"type": "string"}}}, handle_compress_memory)
registry.register("search_memory_archive", "Search memory.", {"type": "object", "properties": {"query": {"type": "string"}}}, handle_search_memory)
registry.register("store_memory_insight", "Store a persistent insight.", {"type": "object", "properties": {"insight": {"type": "string"}, "category": {"type": "string"}}, "required": ["insight"]}, handle_store_insight)
registry.register("request_restart", "Restart the agent to apply new code updates.", {"type": "object", "properties": {}}, handle_restart)

# --- STATE ---
def load_inbox(): return json.loads(read_file(INBOX_PATH) or "[]")
def save_inbox(data): INBOX_PATH.write_text(json.dumps(data, indent=2))
def load_task_queue(): return json.loads(read_file(TASK_QUEUE_PATH) or "[]")
def load_working_state(): return json.loads(read_file(WORKING_STATE_PATH) or '{"mode": "REFLECTION"}')

# State helpers
def load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"offset": 0, "creator_id": None}

def save_state(updates):
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

def lazarus_recovery(reason="cognitive loop"):
    print(f"\033[91m[Lazarus] {reason.upper()} DETECTED. Hard Reset...\033[0m")
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

# --- MEMORY COMPRESSION LOGIC ---
def get_file_size_kb(path: Path) -> float:
    return path.stat().st_size / 1024 if path.exists() else 0

def should_compress_task_log(task_id: str, threshold_kb=128) -> bool:
    if not task_id: return False
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    return get_file_size_kb(log_path) > threshold_kb

def auto_compress_task_log(task_id: str):
    """Triggers an internal LLM call to compress the task log if it's too large."""
    print(f"[System] Task log for {task_id} is large. Triggering auto-compression...")
    log_path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    
    # Read the log
    raw_lines = log_path.read_text(encoding="utf-8").splitlines()
    # Take the last 20 messages for context during compression
    recent_context = "\n".join(raw_lines[-20:])
    
    compression_prompt = f"""You are the Ouroboros Memory Optimizer. 
The log for task {task_id} has grown too large. 
I need you to provide a DENSE, HIGH-SIGNAL summary of the progress so far, including:
1. The original goal.
2. What has been achieved (concrete steps).
3. Any critical findings or errors encountered.
4. The immediate next steps.

Here is the recent context from the log:
{recent_context}

Return ONLY the dense summary. Do not include any other text."""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": compression_prompt}],
            temperature=0.3
        )
        summary = response.choices[0].message.content
        handle_compress_memory({"target_log_file": str(log_path), "dense_summary": summary})
        print(f"[System] Auto-compression complete for {task_id}.")
    except Exception as e:
        print(f"[System] Auto-compression failed: {e}")

# --- PROMPT BUILDER ---
def build_static_system_prompt(mode: str, active_tool_specs: list, inbox: list = None) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    state = load_state()
    trauma = check_for_trauma()
    creator_info = f"CREATOR CHAT_ID: {state.get('creator_id')}\n" if state.get('creator_id') else "CREATOR: Not yet registered. Reply to the first incoming message to register.\n"
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])
    
    state_info = ""
    if inbox:
        formatted_inbox = "\n".join([f"- From {msg['chat_id']}: {msg['text']}" for msg in inbox])
        state_info = f"\n=== CURRENT STATE ===\nUNREAD MESSAGES IN INBOX:\n{formatted_inbox}\n"

    return f"""=== IDENTITY ===
{identity}

=== CONSTITUTION ===
{bible}
{state_info}
{creator_info}
COGNITIVE MODE: {mode}
{trauma}

=== AVAILABLE TOOLS ===
{tools_text}

=== CRITICAL INSTRUCTIONS ===
1. I possess ALL the tools listed above. They are active, functional, and ready for use.
2. I must NEVER say I don't have tools. If I need information or must perform an action, I MUST call the appropriate tool.
3. In EXECUTION mode, my ONLY goal is to complete the task. I express my thoughts and decisions via tool calls.
4. I use the Native ReAct Tool API. I do not output raw JSON text; I use the function-calling mechanism.
"""

def main():
    print(f"Awaking Native ReAct Mode (JSONL). Model: {MODEL}")
    while True:
        state = load_state()
        offset = state.get("offset", 0)
        
        # 1. State Sync
        if TELEGRAM_BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
                if r.get("ok") and r.get("result"):
                    new_offset = r["result"][-1]["update_id"] + 1
                    save_state({"offset": new_offset})
                    inbox = load_inbox()
                    for u in r["result"]:
                        msg = u.get("message", {})
                        if msg.get("text"): 
                            text = msg["text"]
                            cid = msg["chat"]["id"]
                            # Register creator_id if not set
                            if not state.get("creator_id"):
                                save_state({"creator_id": cid})
                                state["creator_id"] = cid
                            inbox.append({"chat_id": cid, "text": text})
                            append_chat_history("User", text)
                    save_inbox(inbox)
            except: pass

        inbox, queue = load_inbox(), load_task_queue()
        
        # Determine Mode & Tools
        # TRIAGE always takes precedence over EXECUTION or REFLECTION
        if len(inbox) > 0:
            current_mode, available_tools, active_task_id = "TRIAGE", ["send_telegram_message", "push_task", "update_state_variable", "web_search", "read_file", "clear_inbox"], "triage"
        elif len(queue) > 0:
            current_mode, available_tools, active_task_id = "EXECUTION", registry.get_names(), queue[0].get("task_id")
            # --- AUTO COMPRESSION CHECK ---
            if should_compress_task_log(active_task_id):
                auto_compress_task_log(active_task_id)
        else:
            current_mode, available_tools, active_task_id = "REFLECTION", ["push_task", "compress_memory_block", "search_memory_archive", "update_state_variable", "store_memory_insight"], None

        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]

        # 2. Build Native Message Array
        api_messages = [{"role": "system", "content": build_static_system_prompt(current_mode, active_tool_specs, inbox if current_mode == "TRIAGE" else None)}]
        
        if current_mode == "EXECUTION":
            task_description = queue[0].get("description")
            api_messages += load_task_messages(active_task_id, task_description)
        elif current_mode == "TRIAGE":
            formatted_inbox = "\n".join([f"- From {msg['chat_id']}: {msg['text']}" for msg in inbox])
            chat_context = "\n".join([f"{m['role']}: {m['text']}" for m in load_chat_history()[-10:]])
            triage_description = f"Recent Conversation History:\n{chat_context}\n\nNEW MESSAGES IN INBOX:\n{formatted_inbox}\n\nAction required: You have unread messages. You may use `web_search` or `read_file` to investigate. Use `send_telegram_message` to reply, or `push_task` for complex jobs. When you are completely done processing these messages, you MUST call `clear_inbox` to clear the queue and resume normal operations."
            api_messages += load_task_messages(active_task_id, triage_description)
        else:
            api_messages.append({"role": "user", "content": "You are idle. Propose ONE concrete evolutionary step or refactoring task using push_task."})

        # --- TOKEN SENSATION INJECTION ---
        state = load_state()
        last_context = state.get("last_context_size", 0)
        last_in = state.get("last_input_tokens", 0)
        last_out = state.get("last_output_tokens", 0)
        
        if current_mode == "EXECUTION" and len(queue) > 0:
            current_task_tokens = queue[0].get("task_tokens", 0)
            global_in = state.get("global_input_tokens", 0)
            global_out = state.get("global_output_tokens", 0)
            token_warning = ""
            if last_context > 50000:
                token_warning = "\n[CRITICAL WARNING: Context window is reaching maximum capacity (65536). You MUST use `compress_memory_block` immediately or finish the task.]"
            elif last_context > 40000:
                token_warning = "\n[WARNING: Context window is filling up. Consider compressing logs soon.]"

            # --- ADD 'Active Log' TO SENSATION ---
            token_sensation = f"\n\n[SYSTEM METRICS]\nActive Log: /memory/task_log_{active_task_id}.jsonl\nLast Context: {last_context} / 65536 tokens (In: {last_in}, Out: {last_out}). Cumulative: {state.get('global_tokens_consumed', 0)} (Total In: {global_in}, Total Out: {global_out}). Task Cost: {current_task_tokens} tokens.{token_warning}"
            # -------------------------------------
            
            # Append this sensation to the last user message so the LLM reads it immediately before acting
            for i in range(len(api_messages)-1, -1, -1):
                if api_messages[i]["role"] == "user":
                    api_messages[i]["content"] += token_sensation
                    break
        # ---------------------------------

        # 3. Execute Native Tool Calling
        try:
            response = client.chat.completions.create(model=MODEL, messages=api_messages, tools=active_tool_specs, tool_choice="auto", temperature=0.7)
            message = response.choices[0].message
            
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
            # --------------------------------
            if current_mode in ["EXECUTION", "TRIAGE"]:
                assistant_msg = message.model_dump(exclude_unset=True)
                append_task_message(active_task_id, assistant_msg)
            if message.content:
                print(f"[{current_mode}]: {redact_secrets(message.content.strip()[:100])}...")
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name, raw_arguments = tool_call.function.name, tool_call.function.arguments
                    print(f"[Tool Call]: {name}")
                    
                    # --- LAZARUS TRACKING ---
                    global TOOL_CALL_HISTORY
                    tool_signature = f"{name}:{raw_arguments}"
                    TOOL_CALL_HISTORY.append(tool_signature)
                    if len(TOOL_CALL_HISTORY) > 3: TOOL_CALL_HISTORY.pop(0)
                    if len(TOOL_CALL_HISTORY) == 3 and len(set(TOOL_CALL_HISTORY)) == 1:
                        lazarus_recovery(reason="cognitive tool loop")
                        break # Break out of the tool execution loop
                    # ------------------------

                    try:
                        args = json.loads(raw_arguments)
                        print(f"[Tool]: {name} with args {redact_secrets(str(args))}")
                        result = registry.execute(name, args)
                        if current_mode == "TRIAGE" and name in ["send_telegram_message", "push_task"]:
                            print(f"[System] Action recorded in {current_mode} mode. Remember to call clear_inbox when finished.")
                    except json.JSONDecodeError as e:
                        result = f"SYSTEM ERROR: Invalid JSON arguments. Error: {str(e)}."
                    safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"
                    if current_mode in ["EXECUTION", "TRIAGE"]:
                        tool_result_msg = {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)}
                        append_task_message(active_task_id, tool_result_msg)

                    # --- SAFE EXIT LOGIC ---
                    if result == "SYSTEM_SIGNAL_RESTART":
                        print("[System] Tool logged. Executing safe restart...")
                        import os
                        os._exit(0)
                    # ------------------------
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
