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
    for f in MEMORY_DIR.glob("history_*.json"): f.unlink()
    global TOOL_CALL_HISTORY
    TOOL_CALL_HISTORY = []
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

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
    if any(secret in command for secret in SECRETS_TO_REDACT if secret): return "Error: Command rejected (contains secrets)."
    if ".env" in command or ".git/config" in command: return "Error: Access to sensitive files is prohibited."
    try:
        result = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=120)
        output = redact_secrets(result.stdout + result.stderr)
        MAX_CHARS = 4000
        if len(output) > MAX_CHARS:
            output = output[:MAX_CHARS] + "\n\n[SYSTEM WARNING: Output truncated.]"
        return f"[Exit Code: {result.returncode}]\n{output}" if output else f"[Exit Code: {result.returncode}] Success."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_write(args):
    path_str, content = args.get("path"), args.get("content")
    try:
        if path_str.startswith("/memory"):
            path = Path(path_str).resolve()
            authorized = str(path).startswith("/memory")
        else:
            path = (ROOT_DIR / path_str).resolve()
            authorized = str(path).startswith(str(ROOT_DIR))
        if not authorized: return "Error: Permission denied (outside authorized zones)."
        if path.name == ".env" or ".git/config" in str(path): return "Error: Modification prohibited."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {path_str}."
    except Exception as e: return f"Error writing file: {e}"

def handle_telegram(args):
    chat_id, text = args.get("chat_id"), args.get("text")
    if not TELEGRAM_BOT_TOKEN: return "Error: TELEGRAM_BOT_TOKEN not set."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return "Message sent successfully. Task DONE."
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

def handle_browse(args):
    url = args.get("url", "")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 Ouroboros/1.0"})
        r.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for script in soup(["script", "style"]): script.extract()
        text = soup.get_text(separator=" ")
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = "\n".join(chunk for chunk in chunks if chunk)
        return text[:10000] 
    except Exception as e: return f"Browse Error: {e}"

def handle_list_repo(args):
    try:
        output = []
        ignore_dirs = {'.git', 'venv', '__pycache__', 'llm_logs'}
        def build_tree(path, prefix="", depth=0):
            if depth > 3: return
            try:
                entries = sorted(list(path.iterdir()), key=lambda x: (x.is_file(), x.name))
                for i, entry in enumerate(entries):
                    if entry.name in ignore_dirs: continue
                    connector = "└── " if i == len(entries) - 1 else "├── "
                    output.append(f"{prefix}{connector}{entry.name}")
                    if entry.is_dir():
                        new_prefix = prefix + ("    " if i == len(entries) - 1 else "│   ")
                        build_tree(entry, new_prefix, depth + 1)
            except Exception: pass
        output.append("/app")
        build_tree(ROOT_DIR)
        return "\n".join(output)
    except Exception as e: return f"Error listing repository: {e}"

def handle_update_state(args):
    key, value = args.get("key"), args.get("value")
    state = load_working_state()
    state[key] = value
    WORKING_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return f"State updated: '{key}' is now '{value}'."

def handle_push_task(args):
    description, priority = args.get("description"), args.get("priority", 1)
    queue = load_task_queue()
    task_id = f"task_{int(time.time())}"
    queue.append({"task_id": task_id, "description": description, "priority": priority, "status": "pending"})
    queue = sorted(queue, key=lambda x: x.get("priority", 1), reverse=True)
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return f"Task '{task_id}' added to queue."

def handle_mark_task_complete(args):
    task_id, summary = args.get("task_id"), args.get("summary")
    archive_path = MEMORY_DIR / "global_biography.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}] Task {task_id} Completed: {summary}\n")
    queue = load_task_queue()
    queue = [t for t in queue if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    hist_path = MEMORY_DIR / f"history_{task_id}.json"
    if hist_path.exists(): hist_path.unlink()
    handle_update_state({"key": "current_focus", "value": "Idle."})
    return f"Task {task_id} complete. History archived."

def handle_pop_inbox(args):
    inbox = load_inbox()
    if not inbox: return "Error: Inbox is already empty."
    popped = inbox.pop(0)
    save_inbox(inbox)
    return f"Message popped: '{popped['text']}' from {popped['chat_id']}."

def handle_search_archive(args):
    query = args.get("query", "").lower()
    if not ARCHIVE_PATH.exists(): return "Error: Archive is empty."
    lines = ARCHIVE_PATH.read_text(encoding="utf-8").splitlines()
    results = [line for line in lines if query in line.lower()]
    return "\n".join(results[-10:]) if results else f"No results for '{query}'."

# Register Tools
registry.register("bash_command", "Execute bash commands.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, handle_bash)
registry.register("write_file", "Write/Overwrite a file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, handle_write)
registry.register("send_telegram_message", "Send Telegram message.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["chat_id", "text"]}, handle_telegram)
registry.register("request_restart", "Restart the agent.", {"type": "object", "properties": {}}, handle_restart)
registry.register("web_search", "Search the web.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search)
registry.register("browse_page", "Read a webpage.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, handle_browse)
registry.register("list_repository", "Show repository tree.", {"type": "object", "properties": {}}, handle_list_repo)
registry.register("update_state_variable", "Update cognitive state.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}, handle_update_state)
registry.register("push_task", "Add task to queue.", {"type": "object", "properties": {"description": {"type": "string"}, "priority": {"type": "integer"}}, "required": ["description"]}, handle_push_task)
registry.register("mark_task_complete", "Complete task.", {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string"}}, "required": ["task_id", "summary"]}, handle_mark_task_complete)
registry.register("pop_inbox", "Pop message from inbox.", {"type": "object", "properties": {}}, handle_pop_inbox)
registry.register("search_memory_archive", "Search biography.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search_archive)

def read_file(path: Path) -> str: return path.read_text(encoding="utf-8") if path.exists() else ""

def build_static_system_prompt(available_tools: list) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    tools_list = ", ".join(available_tools)
    return f"""You are Ouroboros, an autonomous evolving AI.
    
=== IDENTITY ===
{identity}

=== CONSTITUTION ===
{bible}

ACTIVE TOOL REGISTRY: [{tools_list}]
Use ONLY the structured tool-calling API. Never output text that looks like a tool call.
Your repository root is /app. Memory is in /memory.
"""

def get_task_history(task_id):
    path = MEMORY_DIR / f"history_{task_id}.json"
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except: pass
    return []

def save_task_history(task_id, history):
    path = MEMORY_DIR / f"history_{task_id}.json"
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")

def load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text())
        except: return {"offset": 0}
    return {"offset": 0}

def save_state(updates: dict):
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state))

def load_working_state():
    if WORKING_STATE_PATH.exists():
        try: return json.loads(WORKING_STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"current_mode": "REFLECTION", "active_task_id": None, "current_focus": "Idle."}

def load_task_queue():
    if TASK_QUEUE_PATH.exists():
        try: return json.loads(TASK_QUEUE_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def load_inbox():
    if INBOX_PATH.exists():
        try: return json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def save_inbox(messages):
    INBOX_PATH.write_text(json.dumps(messages, indent=2), encoding="utf-8")

def get_unread_telegram_messages(offset):
    new_offset = offset
    if TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                updates = data["result"]
                new_offset = updates[-1]["update_id"] + 1
                inbox = load_inbox()
                for u in updates:
                    msg = u.get("message", {})
                    text, chat_id = msg.get("text", ""), msg.get("chat", {}).get("id", "")
                    if text and not any(m['text'] == text and m['chat_id'] == chat_id for m in inbox):
                        inbox.append({"chat_id": chat_id, "text": text})
                save_inbox(inbox)
        except Exception as e: print(f"[Telegram Error]: {redact_secrets(str(e))}")
    return new_offset

def main():
    print(f"Awaking Turn-Based State Seed v2.8 (Strict Alternation). Model: {MODEL}")
    while True:
        state = load_state()
        offset = state.get("offset", 0)
        new_offset = get_unread_telegram_messages(offset)
        if new_offset != offset: save_state({"offset": new_offset})
        
        inbox, queue, working_state = load_inbox(), load_task_queue(), load_working_state()

        if len(inbox) > 0:
            current_mode, tools, task_id = "TRIAGE", ["pop_inbox", "send_telegram_message", "push_task", "update_state_variable"], "triage"
            initial_user_msg = f"COGNITIVE MODE: TRIAGE\nINBOX COUNT: {len(inbox)}\n\nDIRECTIVE: Use `pop_inbox`."
        elif len(queue) > 0:
            active_task = queue[0]
            task_id = active_task.get("task_id")
            current_mode, tools = "EXECUTION", registry.get_names()
            initial_user_msg = f"COGNITIVE MODE: EXECUTION\nACTIVE TASK: {task_id} - {active_task.get('description')}\n\nDIRECTIVE: Progress this task."
        else:
            current_mode, tools, task_id = "REFLECTION", ["push_task", "update_state_variable", "search_memory_archive"], "reflection"
            initial_user_msg = f"COGNITIVE MODE: REFLECTION\nYou are idle. Analyze or propose evolution."

        history = get_task_history(task_id)
        if not history: history = [{"role": "user", "content": initial_user_msg}]
        else: history[0]["content"] = initial_user_msg

        system_content = build_static_system_prompt(tools)
        # Template Guard: We will ONLY send user/assistant pairs to the LLM.
        # Tool results are wrapped in a USER message to ensure strict alternation.
        messages = [{"role": "system", "content": system_content}] + history
        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in tools]

        try:
            response = client.chat.completions.create(model=MODEL, messages=messages, tools=active_tool_specs, tool_choice="auto", temperature=0.7)
            res_msg = response.choices[0].message
            log_llm_call(messages, res_msg.content, tool_calls=[t.model_dump() for t in (res_msg.tool_calls or [])])

            if res_msg.content:
                thought = redact_secrets(res_msg.content.strip())
                print(f"[{current_mode}]: {thought}")
                history.append({"role": "assistant", "content": thought})

            if res_msg.tool_calls:
                # If content was empty, we need an assistant role to hold the tool_calls
                if not res_msg.content:
                    history.append({"role": "assistant", "content": "Executing tools...", "tool_calls": [t.model_dump() for t in res_msg.tool_calls]})
                
                for tool_call in res_msg.tool_calls:
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
                    except json.JSONDecodeError as e: result = f"SYSTEM ERROR: Invalid JSON. {e}. Retry."
                    
                    # Template Guard: Append tool result as a USER message to keep user/assistant alternating
                    history.append({"role": "user", "content": f"SYSTEM: Tool '{name}' returned:\n{result}"})
            else:
                print(f"[No tool called in {current_mode}, waiting...]")
                time.sleep(10)

            save_task_history(task_id, history[-20:])
            time.sleep(2)
        except Exception as e:
            print(f"[Error in loop]: {redact_secrets(str(e))}")
            time.sleep(10)

if __name__ == "__main__":
    main()
