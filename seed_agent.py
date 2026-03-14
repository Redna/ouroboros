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

client = OpenAI(base_url=API_BASE, api_key=API_KEY, timeout=600.0)

# --- UTILS ---
def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

# --- LLM CALL LOGGER ---
def log_llm_call(messages, response_content, tool_calls=None):
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = LLM_LOG_DIR / f"call-{timestamp}-{int(time.time())}.json"
        log_data = {"timestamp": timestamp, "model": MODEL, "messages": messages, "response": response_content, "tool_calls": tool_calls}
        log_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception as e: print(f"[Logger Error]: {e}")

# --- SECRET REDACTION ---
SECRETS_TO_REDACT = []
if TELEGRAM_BOT_TOKEN: SECRETS_TO_REDACT.append(TELEGRAM_BOT_TOKEN)
if GITHUB_TOKEN: SECRETS_TO_REDACT.append(GITHUB_TOKEN)

def redact_secrets(text: str) -> str:
    if not text: return text
    for secret in SECRETS_TO_REDACT:
        if secret: text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)
    return text

# --- SELF-HEALING (LAZARUS PROTOCOL) ---
TOOL_CALL_HISTORY = []
MAX_REPETITIONS = 3

def lazarus_recovery(reason="cognitive loop"):
    print(f"\033[91m[Lazarus] {reason.upper()} DETECTED. Executing emergency recovery...\033[0m")
    try:
        with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n\n--- LAZARUS RECOVERY ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\nReason: {reason}\n")
    except: pass
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
    save_inbox([])
    global TOOL_CALL_HISTORY
    TOOL_CALL_HISTORY = []
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

# --- TASK-BOUND JSONL MEMORY ---
def load_task_messages(active_task_id: str, description: str) -> list:
    if not active_task_id: return []
    log_path = MEMORY_DIR / f"task_log_{active_task_id}.jsonl"
    messages = []
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip(): messages.append(json.loads(line))
    if not messages:
        first_msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        messages.append(first_msg)
        append_task_message(active_task_id, first_msg)
    return messages

def append_task_message(active_task_id: str, message_dict: dict):
    if not active_task_id: return
    log_path = MEMORY_DIR / f"task_log_{active_task_id}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")

# --- CONTEXT BUILDER ---
def build_context_block(mode: str) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    return f"""=== IDENTITY ===\n{identity}\n=== CONSTITUTION ===\n{bible}\n
COGNITIVE MODE: {mode}
CRITICAL INSTRUCTION: You must strictly use the provided tool-calling API. Do not output raw text blocks when an action is required."""

# --- TEMPLATE VIRTUALIZATION v2 ---
def prepare_api_messages(api_messages: list, context_block: str) -> list:
    """Transforms all roles into a strict User/Assistant sequence, starting with a User role containing the context."""
    virtual = []
    
    # 1. Start with User Role containing Context + First User Message
    # We remove the "system" role entirely to avoid triggering index shifts in rigid local templates.
    first_msg_content = context_block + "\n\n"
    
    for msg in api_messages:
        role = msg.get("role")
        if role == "system": continue # Skip system role
        
        content = msg.get("content") or ""
        # Map Tool Result to User role
        if role == "tool":
            role = "user"
            content = f"SYSTEM (Tool Result): {content}"
        
        if not virtual:
            # First real message after system is skipped
            virtual.append({"role": "user", "content": first_msg_content + content})
        else:
            # Collapse Consecutive Roles
            if virtual[-1]["role"] == role:
                virtual[-1]["content"] += f"\n\n{content}"
            else:
                virtual.append({"role": role, "content": content})
            
    # 2. Final Alternation Guard
    # If the last role is assistant, add user heartbeat to satisfy ns.index % 2 == 0 logic.
    if virtual and virtual[-1]["role"] == "assistant":
        virtual.append({"role": "user", "content": "Acknowledged. Proceed."})
        
    return virtual

# --- TOOL REGISTRY ---
class ToolRegistry:
    def __init__(self): self.tools = {}
    def register(self, name, description, parameters, handler): 
        self.tools[name] = {"spec": {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}, "handler": handler}
    def get_specs(self): return [tool["spec"] for tool in self.tools.values()]
    def get_names(self): return list(self.tools.keys())
    def execute(self, name, args):
        if name in self.tools:
            try: return self.tools[name]["handler"](args)
            except Exception as e: return f"Execution Error in tool '{name}': {e}"
        return f"Error: Tool '{name}' not found."

registry = ToolRegistry()

# --- TOOL HANDLERS ---
def handle_bash(args):
    command = args.get("command", "")
    if any(secret in command for secret in SECRETS_TO_REDACT if secret): return "Error: rejected (secrets)."
    try:
        result = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=120)
        output = redact_secrets(result.stdout + result.stderr)
        MAX_CHARS = 4000
        if len(output) > MAX_CHARS: output = output[:MAX_CHARS] + "\n\n[SYSTEM WARNING: Output truncated.]"
        return f"[Exit Code: {result.returncode}]\n{output}" if output else f"[Exit Code: {result.returncode}] Success."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_write(args):
    path_str, content = args.get("path"), args.get("content")
    try:
        path = (ROOT_DIR / path_str).resolve() if not path_str.startswith("/memory") else Path(path_str).resolve()
        if not (str(path).startswith(str(ROOT_DIR)) or str(path).startswith("/memory")): return "Error: Permission denied."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {path_str}."
    except Exception as e: return f"Error writing file: {e}"

def handle_telegram(args):
    chat_id, text = args.get("chat_id"), args.get("text")
    if not TELEGRAM_BOT_TOKEN: return "Error: bot token not set."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return "Message sent successfully."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_restart(args):
    print("[Requesting Restart] Exiting...")
    os._exit(0)

def handle_search(args):
    query = args.get("query", "")
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])[:5]
        output = "\n".join([f"- {res['title']} ({res['url']}): {res.get('content', '')}" for res in results])
        return output if output else "No results found."
    except Exception as e: return f"Search Error: {e}"

def handle_list_repo(args):
    try:
        output = []
        for p in ROOT_DIR.rglob('*'):
            if any(part in p.parts for part in {'.git', 'venv', '__pycache__', 'llm_logs'}): continue
            output.append(str(p.relative_to(ROOT_DIR)))
        return "\n".join(output)
    except Exception as e: return f"Error: {e}"

def handle_update_state(args):
    key, value = args.get("key"), args.get("value")
    state = load_working_state(); state[key] = value
    WORKING_STATE_PATH.write_text(json.dumps(state, indent=2))
    return f"State updated: '{key}' is now '{value}'."

def handle_push_task(args):
    description, priority = args.get("description"), args.get("priority", 1)
    queue = load_task_queue(); task_id = f"task_{int(time.time())}"
    queue.append({"task_id": task_id, "description": description, "priority": priority, "status": "pending"})
    queue = sorted(queue, key=lambda x: x.get("priority", 1), reverse=True)
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2))
    return f"Task '{task_id}' added to queue."

def handle_mark_task_complete(args):
    task_id, summary = args.get("task_id"), args.get("summary")
    archive_path = MEMORY_DIR / "global_biography.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}] Task {task_id} Completed: {summary}\n")
    queue = [t for t in load_task_queue() if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2))
    return f"Task {task_id} marked complete."

# --- INBOX HANDLING ---
def load_inbox():
    if INBOX_PATH.exists():
        try: return json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def save_inbox(messages):
    INBOX_PATH.write_text(json.dumps(messages, indent=2), encoding="utf-8")

# --- SEARCH TOOL ---
def handle_search_archive(args):
    query = args.get("query", "").lower()
    if not ARCHIVE_PATH.exists(): return "Error: Archive is empty."
    results = [line for line in ARCHIVE_PATH.read_text(encoding="utf-8").splitlines() if query in line.lower()]
    return "\n".join(results[-10:]) if results else f"No results for '{query}'."

# Register Tools
registry.register("bash_command", "Execute bash.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, handle_bash)
registry.register("write_file", "Write file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, handle_write)
registry.register("send_telegram_message", "Telegram.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["chat_id", "text"]}, handle_telegram)
registry.register("request_restart", "Restart.", {"type": "object", "properties": {}}, handle_restart)
registry.register("web_search", "Search web.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search)
registry.register("list_repository", "Show repo.", {"type": "object", "properties": {}}, handle_list_repo)
registry.register("update_state_variable", "Update state.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}, handle_update_state)
registry.register("push_task", "Add task.", {"type": "object", "properties": {"description": {"type": "string"}, "priority": {"type": "integer"}}, "required": ["description"]}, handle_push_task)
registry.register("mark_task_complete", "Complete.", {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}, "required": ["task_id", "summary"]}, handle_mark_task_complete)
registry.register("search_memory_archive", "Search biography.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search_archive)

def load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text())
        except: return {"offset": 0}
    return {"offset": 0}

def save_state(updates: dict):
    state = load_state(); state.update(updates)
    STATE_PATH.write_text(json.dumps(state))

def load_working_state():
    if WORKING_STATE_PATH.exists():
        try: return json.loads(WORKING_STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"current_mode": "REFLECTION"}

def load_task_queue():
    if TASK_QUEUE_PATH.exists():
        try: return json.loads(TASK_QUEUE_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def get_unread_telegram_messages(offset):
    new_offset = offset
    if TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                updates = data["result"]; new_offset = updates[-1]["update_id"] + 1
                inbox = load_inbox()
                for u in updates:
                    msg = u.get("message", {}); text, chat_id = msg.get("text", ""), msg.get("chat", {}).get("id", "")
                    if text: inbox.append({"chat_id": chat_id, "text": text})
                save_inbox(inbox)
        except Exception: pass
    return new_offset

def main():
    print(f"Awaking Naked-Role State Seed v3.4. Model: {MODEL}")
    while True:
        state = load_state()
        offset = state.get("offset", 0)
        new_offset = get_unread_telegram_messages(offset)
        if new_offset != offset: save_state({"offset": new_offset})
        
        inbox_messages, task_queue, working_state = load_inbox(), load_task_queue(), load_working_state()

        if len(inbox_messages) > 0:
            current_mode, available_tools, active_task_id = "TRIAGE", ["send_telegram_message", "push_task", "update_state_variable"], None
            formatted_inbox = "\n".join([f"- [{msg['chat_id']}] {msg['text']}" for msg in inbox_messages])
            api_messages = [{"role": "user", "content": f"You have unread messages:\n{formatted_inbox}\nAction: Reply or push to queue."}]
        elif len(task_queue) > 0:
            current_mode, available_tools, active_task_id = "EXECUTION", registry.get_names(), task_queue[0].get("task_id")
            api_messages = load_task_messages(active_task_id, task_queue[0].get("description"))
        else:
            current_mode, available_tools, active_task_id = "REFLECTION", ["push_task", "update_state_variable", "search_memory_archive"], None
            api_messages = [{"role": "user", "content": "You are idle. Propose ONE evolutionary task using push_task."}]

        # VIRTUAL TEMPLATE GUARD v2: 
        # Remove "system" role, start with User (Context + Prompt), collapse, and alternate.
        context_block = build_context_block(current_mode)
        api_messages = prepare_api_messages(api_messages, context_block)
        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]

        try:
            response = client.chat.completions.create(model=MODEL, messages=api_messages, tools=active_tool_specs, tool_choice="auto", temperature=0.7)
            message = response.choices[0].message
            log_llm_call(api_messages, message.content, tool_calls=[t.model_dump() for t in (message.tool_calls or [])])
            
            if current_mode == "EXECUTION":
                append_task_message(active_task_id, message.model_dump(exclude_unset=True))

            if message.content:
                thought = redact_secrets(message.content.strip())
                print(f"[{current_mode}]: {thought}")

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name, raw_args = tool_call.function.name, tool_call.function.arguments
                    print(f"[Tool Call]: {name}")
                    
                    tool_signature = f"{name}:{raw_args}"
                    TOOL_CALL_HISTORY.append(tool_signature)
                    if len(TOOL_CALL_HISTORY) > 3: TOOL_CALL_HISTORY.pop(0)
                    if len(TOOL_CALL_HISTORY) == 3 and len(set(TOOL_CALL_HISTORY)) == 1:
                        lazarus_recovery(reason="tool execution loop")
                        break
                    
                    try:
                        args = json.loads(raw_args)
                        result = registry.execute(name, args)
                        if current_mode == "TRIAGE" and name in ["send_telegram_message", "push_task"]:
                            if "Error" not in str(result): save_inbox([]); print(f"[System] Cleared inbox.")
                    except json.JSONDecodeError as e: result = f"SYSTEM ERROR: Invalid JSON. {e}."
                        
                    if current_mode == "EXECUTION":
                        append_task_message(active_task_id, {"role": "tool", "tool_call_id": tool_call.id, "name": name, "content": str(result)})
            else:
                print(f"[Waiting in {current_mode}...]")
                time.sleep(10)
            time.sleep(2)
        except Exception as e:
            print(f"[Error in loop]: {redact_secrets(str(e))}")
            time.sleep(10)

if __name__ == "__main__": main()
