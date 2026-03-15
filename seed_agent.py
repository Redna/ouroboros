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
    
    # Keep only the last 15 messages to ensure system prompt dominance
    raw_messages = raw_messages[-15:]
    
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
        nudge = {"role": "user", "content": "Please proceed with your next action using a tool."}
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
    try:
        r = subprocess.run(args.get("command", ""), shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60)
        out = redact_secrets(r.stdout + r.stderr)
        return out[:4000] if out else "Success."
    except Exception as e: return str(e)

def handle_write(args):
    try:
        p = Path(args.get("path")).resolve()
        Path(p.parent).mkdir(parents=True, exist_ok=True)
        p.write_text(args.get("content", ""), encoding="utf-8")
        return f"Wrote {p.name}."
    except Exception as e: return str(e)

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

def handle_pop_inbox(args):
    inbox = load_inbox()
    if not inbox: return "Empty."
    p = inbox.pop(0); save_inbox(inbox)
    return f"Msg: {p['text']} from {p['chat_id']}"

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
            # For JSONL files, we must write a valid JSON object per line.
            # We use a system role message to store the compression metadata.
            compressed_msg = {
                "role": "system",
                "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}"
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

def handle_restart(args):
    """Returns a signal rather than killing the process immediately."""
    return "SYSTEM_SIGNAL_RESTART"

registry.register("bash_command", "Execute bash.", {"type": "object", "properties": {"command": {"type": "string"}}}, handle_bash)
registry.register("write_file", "Write file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, handle_write)
registry.register("send_telegram_message", "Telegram.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}}, handle_telegram)
registry.register("push_task", "Queue task.", {"type": "object", "properties": {"description": {"type": "string"}}}, handle_push_task)
registry.register("pop_inbox", "Pop msg.", {"type": "object", "properties": {}}, handle_pop_inbox)
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

# --- PROMPT BUILDER ---
def build_static_system_prompt(mode: str, active_tool_specs: list) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    state = load_state()
    trauma = check_for_trauma()
    creator_info = f"CREATOR CHAT_ID: {state.get('creator_id')}\n" if state.get('creator_id') else "CREATOR: Not yet registered. Reply to the first incoming message to register.\n"
    tools_text = "\n".join([f"- {t['function']['name']}: {t['function']['description']}" for t in active_tool_specs])
    
    return f"""=== IDENTITY ===
{identity}
=== CONSTITUTION ===
{bible}

{creator_info}
COGNITIVE MODE: {mode}
{trauma}

AVAILABLE TOOLS IN THIS MODE:
{tools_text}

CRITICAL INSTRUCTION: You must strictly use the provided tool-calling API to interact with the world. Do not output raw text blocks when an action is required."""

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
            current_mode, available_tools, active_task_id = "TRIAGE", ["send_telegram_message", "push_task", "update_state_variable", "web_search"], None
        elif len(queue) > 0:
            current_mode, available_tools, active_task_id = "EXECUTION", registry.get_names(), queue[0].get("task_id")
        else:
            current_mode, available_tools, active_task_id = "REFLECTION", ["push_task", "compress_memory_block", "search_memory_archive", "update_state_variable"], None

        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]

        # 2. Build Native Message Array
        api_messages = [{"role": "system", "content": build_static_system_prompt(current_mode, active_tool_specs)}]
        
        if current_mode == "EXECUTION":
            task_description = queue[0].get("description")
            api_messages += load_task_messages(active_task_id, task_description)
        elif current_mode == "TRIAGE":
            formatted_inbox = "\n".join([f"- From {msg['chat_id']}: {msg['text']}" for msg in inbox])
            chat_context = "\n".join([f"{m['role']}: {m['text']}" for m in load_chat_history()[-10:]])
            api_messages.append({
                "role": "user", 
                "content": f"Recent Conversation History:\n{chat_context}\n\nNEW MESSAGES IN INBOX:\n{formatted_inbox}\n\nAction required: You have unread messages. Use `send_telegram_message` with the CORRECT chat_id from the inbox to reply, or use `push_task` for complex jobs."
            })
        else:
            api_messages.append({"role": "user", "content": "You are idle. Propose ONE concrete evolutionary step or refactoring task using push_task."})

        # 3. Execute Native Tool Calling
        try:
            response = client.chat.completions.create(model=MODEL, messages=api_messages, tools=active_tool_specs, tool_choice="auto", temperature=0.7)
            message = response.choices[0].message
            if current_mode == "EXECUTION":
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
                            save_inbox([]); print(f"[System] Inbox Cleared in {current_mode} mode.")
                    except json.JSONDecodeError as e:
                        result = f"SYSTEM ERROR: Invalid JSON arguments. Error: {str(e)}."
                    safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"
                    if current_mode == "EXECUTION":
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
                time.sleep(10)
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
            
            time.sleep(10)

if __name__ == "__main__": main()
