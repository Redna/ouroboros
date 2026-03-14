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

# New State Files
WORKING_STATE_PATH = MEMORY_DIR / "working_state.json"
TASK_QUEUE_PATH = MEMORY_DIR / "task_queue.json"
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
        log_data = {
            "timestamp": timestamp,
            "model": MODEL,
            "messages": messages,
            "response": response_content,
            "tool_calls": tool_calls
        }
        log_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Logger Error]: {e}")

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
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n--- LAZARUS RECOVERY ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\nReason: {reason}\n")
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
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
            output = output[:MAX_CHARS] + f"\n\n[SYSTEM WARNING: Output truncated. Length exceeded {MAX_CHARS} characters. Use 'grep' or 'head'.]"
            
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
        result = subprocess.run("tree -L 3 -I 'venv|__pycache__|.git' /app", shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            result = subprocess.run("ls -R /app | grep -vE 'venv|__pycache__|.git'", shell=True, capture_output=True, text=True)
        return result.stdout or "Empty repository."
    except Exception as e: return f"Error listing repository: {e}"

def handle_update_state(args):
    """Updates a specific key in the working state."""
    key, value = args.get("key"), args.get("value")
    state = {}
    if WORKING_STATE_PATH.exists():
        try: state = json.loads(WORKING_STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError: pass
            
    state[key] = value
    WORKING_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return f"State updated: '{key}' is now '{value}'."

def handle_push_task(args):
    """Pushes a new task to the queue."""
    description, priority = args.get("description"), args.get("priority", 1)
    queue = []
    if TASK_QUEUE_PATH.exists():
        try: queue = json.loads(TASK_QUEUE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError: pass
            
    task_id = f"task_{int(time.time())}"
    queue.append({"task_id": task_id, "description": description, "priority": priority, "status": "pending"})
    queue = sorted(queue, key=lambda x: x.get("priority", 1), reverse=True)
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    
    return f"Task '{task_id}' added to queue with priority {priority}."

def handle_mark_task_complete(args):
    """Completes a task, archives its summary, and removes it from the queue."""
    task_id, summary = args.get("task_id"), args.get("summary")
    
    archive_path = MEMORY_DIR / "global_biography.md"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}] Task {task_id} Completed: {summary}\n")
        
    if TASK_QUEUE_PATH.exists():
        try:
            queue = json.loads(TASK_QUEUE_PATH.read_text(encoding="utf-8"))
            queue = [t for t in queue if t.get("task_id") != task_id]
            TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        except json.JSONDecodeError: pass
            
    handle_update_state({"key": "current_focus", "value": "Idle. Awaiting next task."})
    return f"Task {task_id} marked complete and summarized."

def handle_compress_memory(args):
    """Allows the agent to actively compress a verbose file into a summary."""
    target_file, dense_summary = args.get("target_log_file"), args.get("dense_summary")
    path = Path(target_file).resolve()
    
    if not str(path).startswith(str(MEMORY_DIR)):
        return "Error: Permission denied. Can only compress files in /memory."
    if not path.exists():
        return f"Error: File {target_file} not found."
        
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        compressed_content = f"--- COMPRESSED LOG ({timestamp}) ---\n{dense_summary}\n"
        path.write_text(compressed_content, encoding="utf-8")
        return f"Successfully compressed {path.name} to save token space."
    except Exception as e:
        return f"Error compressing file: {e}"

# Register Tools
registry.register("bash_command", "Execute bash commands.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, handle_bash)
registry.register("write_file", "Write/Overwrite a file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, handle_write)
registry.register("send_telegram_message", "Send Telegram message.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["chat_id", "text"]}, handle_telegram)
registry.register("request_restart", "Restart the agent.", {"type": "object", "properties": {}}, handle_restart)
registry.register("web_search", "Search the web via SearXNG.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search)
registry.register("browse_page", "Read the text content of a webpage.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, handle_browse)
registry.register("list_repository", "Show the hierarchical tree of the repository files.", {"type": "object", "properties": {}}, handle_list_repo)

registry.register("update_state_variable", "Update your current cognitive state or focus.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}, handle_update_state)
registry.register("push_task", "Add a new task to your execution queue.", {"type": "object", "properties": {"description": {"type": "string"}, "priority": {"type": "integer"}}, "required": ["description"]}, handle_push_task)
registry.register("mark_task_complete", "Close an active task and append its summary to your biography.", {"type": "object", "properties": {"task_id": {"type": "string"}, "summary": {"type": "string", "description": "Dense 1-2 sentence summary."}}, "required": ["task_id", "summary"]}, handle_mark_task_complete)
registry.register("compress_memory_block", "Replace a long log file with a synthesized summary.", {"type": "object", "properties": {"target_log_file": {"type": "string", "description": "Absolute path to the log file in /memory"}, "dense_summary": {"type": "string"}}, "required": ["target_log_file", "dense_summary"]}, handle_compress_memory)

def read_file(path: Path) -> str: return path.read_text(encoding="utf-8") if path.exists() else ""

def build_dynamic_prompt(mode: str, state: dict, inbox: list, queue: list, task_log: str, available_tools: list) -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    base_identity = f"Your repository root is `/app`. (Tracked by Git)\nYour isolated memory volume is `/memory`. (NOT tracked by Git)\n\n=== IDENTITY ===\n{identity}\n=== CONSTITUTION ===\n{bible}\n\n"
    
    tools_list = ", ".join(available_tools)
    base_identity += f"ACTIVE TOOL REGISTRY: [{tools_list}]\nUse the structured tool-calling API to interact with these tools.\n\n"

    if mode == "TRIAGE":
        formatted_inbox = "\n".join([f"- [{msg['chat_id']}] {msg['text']}" for msg in inbox])
        return base_identity + f"""COGNITIVE MODE: TRIAGE
WORKING STATE Focus: {state.get('current_focus', 'None')}

UNREAD INBOX:
{formatted_inbox}

DIRECTIVE:
1. You are interrupted. Read the inbox.
2. If asking a quick question, use `send_telegram_message` to reply.
3. If receiving a new command, use `push_task` to queue it.
4. Do NOT execute code or write files. Clear the inbox to return to work.
"""
    elif mode == "EXECUTION":
        active_task = queue[0] if queue else {"description": "None"}
        return base_identity + f"""COGNITIVE MODE: EXECUTION
WORKING STATE Focus: {state.get('current_focus', 'None')}

ACTIVE TASK:
ID: {active_task.get('task_id', 'N/A')}
Description: {active_task.get('description', 'N/A')}

RECENT TASK LOG:
{task_log}

DIRECTIVE:
1. Use your tools to make progress on the Active Task.
2. Update your working state using `update_state_variable` if focus shifts.
3. If finished, call `mark_task_complete` with a dense summary.
4. If too large, use `push_task` to break it into sub-tasks.
"""
    elif mode == "REFLECTION":
        return base_identity + f"""COGNITIVE MODE: REFLECTION
Your inbox is clear and your task queue is empty. You are idle.

DIRECTIVE:
1. Act on initiative (Principle P0 & P2).
2. Analyze your codebase, architecture, or memory state.
3. Use `push_task` to propose ONE concrete evolutionary step.
4. Use `compress_memory_block` if old logs are too large.
"""
    return base_identity + "Error: Unknown cognitive mode."

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
    return {"current_mode": "REFLECTION", "active_task_id": None, "current_focus": "Idle. Awaiting next task."}

def load_task_queue():
    if TASK_QUEUE_PATH.exists():
        try: return json.loads(TASK_QUEUE_PATH.read_text(encoding="utf-8"))
        except: pass
    return []

def read_current_task_log(active_task_id):
    if not active_task_id:
        return "No active task log."
    log_path = MEMORY_DIR / f"task_log_{active_task_id}.txt"
    if log_path.exists():
        content = log_path.read_text(encoding="utf-8")
        if len(content) > 10000:
            return f"...[Truncated]...\n{content[-10000:]}"
        return content
    return "Task log is empty."

def append_to_task_log(active_task_id, content):
    if not active_task_id: return
    log_path = MEMORY_DIR / f"task_log_{active_task_id}.txt"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(content + "\n")

def get_unread_telegram_messages(offset):
    messages = []
    new_offset = offset
    if TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"):
                updates = data["result"]
                new_offset = updates[-1]["update_id"] + 1
                for u in updates:
                    msg = u.get("message", {})
                    text, chat_id = msg.get("text", ""), msg.get("chat", {}).get("id", "")
                    if text: 
                        messages.append({"chat_id": chat_id, "text": text})
        except Exception as e: print(f"[Telegram Error]: {redact_secrets(str(e))}")
    return messages, new_offset


def main():
    print(f"Awaking State-Driven Seed v2.0. Model: {MODEL}")

    while True:
        state = load_state()
        offset = state.get("offset", 0)
        
        inbox_messages, new_offset = get_unread_telegram_messages(offset)
        if new_offset != offset:
            save_state({"offset": new_offset})
            
        task_queue = load_task_queue()
        working_state = load_working_state()

        # Determine Cognitive Mode & Restrict Tools
        if len(inbox_messages) > 0:
            current_mode = "TRIAGE"
            available_tools = ["send_telegram_message", "push_task", "update_state_variable"]
            active_task_id = None
        elif len(task_queue) > 0:
            current_mode = "EXECUTION"
            available_tools = registry.get_names()
            active_task = task_queue[0]
            active_task_id = active_task.get("task_id")
            if working_state.get("active_task_id") != active_task_id:
                handle_update_state({"key": "active_task_id", "value": active_task_id})
                working_state["active_task_id"] = active_task_id
        else:
            current_mode = "REFLECTION"
            available_tools = ["push_task", "compress_memory_block", "update_state_variable"]
            active_task_id = None

        if working_state.get("current_mode") != current_mode:
            handle_update_state({"key": "current_mode", "value": current_mode})
            working_state["current_mode"] = current_mode

        active_tool_specs = [t for t in registry.get_specs() if t['function']['name'] in available_tools]

        system_content = build_dynamic_prompt(
            mode=current_mode, 
            state=working_state, 
            inbox=inbox_messages, 
            queue=task_queue, 
            task_log=read_current_task_log(active_task_id),
            available_tools=available_tools
        )

        loop_messages = [{"role": "system", "content": system_content}, {"role": "user", "content": "What is your next action? Respond ONLY with tool calls if possible, or a brief thought followed by a tool call."}]

        try:
            response = client.chat.completions.create(model=MODEL, messages=loop_messages, tools=active_tool_specs, tool_choice="auto", temperature=0.7)
            message = response.choices[0].message
            
            # --- LOG LLM CALL ---
            log_llm_call(loop_messages, message.content, tool_calls=[t.model_dump() for t in (message.tool_calls or [])])

            if message.content:
                thought = redact_secrets(message.content.strip())
                while thought.lower().startswith("thought:"): thought = thought[8:].strip()
                print(f"[{current_mode}]: {thought}")
                
                # Append to active task log if executing
                if current_mode == "EXECUTION":
                    append_to_task_log(active_task_id, f"Thought: {thought}")

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    raw_arguments = tool_call.function.arguments
                    print(f"[Tool Call]: {name}")
                    
                    tool_signature = f"{name}:{raw_arguments}"
                    TOOL_CALL_HISTORY.append(tool_signature)
                    if len(TOOL_CALL_HISTORY) > MAX_REPETITIONS: TOOL_CALL_HISTORY.pop(0)
                    if len(TOOL_CALL_HISTORY) == MAX_REPETITIONS and len(set(TOOL_CALL_HISTORY)) == 1:
                        lazarus_recovery(reason="tool execution loop")
                        break

                    try:
                        args = json.loads(raw_arguments)
                        result = registry.execute(name, args)
                    except json.JSONDecodeError as e:
                        result = f"SYSTEM ERROR: Invalid JSON arguments provided to tool '{name}'. Error details: {str(e)}. Please correct your JSON syntax and try again."

                    if current_mode == "EXECUTION":
                        append_to_task_log(active_task_id, f"[Tool Call: {name}]\nArguments: {json.dumps(args, indent=2) if isinstance(args, dict) else raw_arguments}\nResult: {result}")
            else:
                print(f"[No tool called in {current_mode}, waiting...]")
                time.sleep(10)
            time.sleep(2)
        except Exception as e:
            print(f"[Error in loop]: {redact_secrets(str(e))}")
            time.sleep(10)

if __name__ == "__main__":
    main()
