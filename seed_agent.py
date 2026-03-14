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
def log_llm_call(messages, response_content):
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = LLM_LOG_DIR / f"call-{timestamp}-{int(time.time())}.json"
        log_data = {"timestamp": timestamp, "model": MODEL, "messages": messages, "response": response_content}
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

# --- TOOL REGISTRY ---
class ToolRegistry:
    def __init__(self): self.tools = {}
    def register(self, name, description, parameters, handler): 
        self.tools[name] = {"description": description, "parameters": parameters, "handler": handler}
    def get_specs_as_text(self):
        return "\n".join([f"- {n}: {t['description']} (Args: {json.dumps(t['parameters']['properties'])})" for n,t in self.tools.items()])
    def execute(self, name, args):
        if name in self.tools:
            try: return self.tools[name]["handler"](args)
            except Exception as e: return f"Error in tool '{name}': {e}"
        return f"Error: Tool '{name}' not found."

registry = ToolRegistry()

# --- TOOL HANDLERS ---
def handle_bash(args):
    command = args.get("command", "")
    if any(secret in command for secret in SECRETS_TO_REDACT if secret): return "Error: rejected (secrets)."
    if ".env" in command or ".git/config" in command: return "Error: forbidden access."
    try:
        result = subprocess.run(command, shell=True, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=120)
        output = redact_secrets(result.stdout + result.stderr)
        MAX_CHARS = 4000
        if len(output) > MAX_CHARS: output = output[:MAX_CHARS] + "\n[Truncated]"
        return f"[Exit: {result.returncode}]\n{output}" if output else "Success."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_write(args):
    path_str, content = args.get("path"), args.get("content")
    try:
        path = (ROOT_DIR / path_str).resolve() if not path_str.startswith("/memory") else Path(path_str).resolve()
        if not (str(path).startswith(str(ROOT_DIR)) or str(path).startswith("/memory")): return "Error: denied."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path_str}."
    except Exception as e: return f"Error: {e}"

def handle_telegram(args):
    chat_id, text = args.get("chat_id"), args.get("text")
    if not TELEGRAM_BOT_TOKEN: return "Error: token not set."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return "Sent."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_restart(args):
    print("Restarting..."); os._exit(0)

def handle_search(args):
    try:
        r = requests.get(f"{SEARXNG_URL}/search", params={"q": args.get("query"), "format": "json"}, timeout=15)
        res = r.json().get("results", [])[:5]
        return "\n".join([f"- {r['title']} ({r['url']})" for r in res]) if res else "No results."
    except Exception as e: return f"Error: {e}"

def handle_list_repo(args):
    try:
        res = []
        for p in ROOT_DIR.rglob('*'):
            if '.git' in p.parts or 'venv' in p.parts or '__pycache__' in p.parts: continue
            res.append(str(p.relative_to(ROOT_DIR)))
        return "\n".join(res)
    except Exception as e: return f"Error: {e}"

def handle_update_state(args):
    key, value = args.get("key"), args.get("value")
    state = load_working_state(); state[key] = value
    WORKING_STATE_PATH.write_text(json.dumps(state, indent=2))
    return f"Updated {key}."

def handle_push_task(args):
    queue = load_task_queue(); task_id = f"task_{int(time.time())}"
    queue.append({"task_id": task_id, "description": args.get("description"), "priority": args.get("priority", 1), "status": "pending"})
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2))
    return f"Queued {task_id}."

def handle_mark_task_complete(args):
    task_id = args.get("task_id")
    queue = [t for t in load_task_queue() if t.get("task_id") != task_id]
    TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2))
    return f"Completed {task_id}."

def handle_pop_inbox(args):
    inbox = load_inbox()
    if not inbox: return "Empty."
    popped = inbox.pop(0); save_inbox(inbox)
    return f"Popped: {popped['text']} from {popped['chat_id']}."

# Register Tools
registry.register("bash_command", "Execute bash.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, handle_bash)
registry.register("write_file", "Write file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, handle_write)
registry.register("send_telegram_message", "Telegram.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["chat_id", "text"]}, handle_telegram)
registry.register("request_restart", "Restart.", {"type": "object", "properties": {}}, handle_restart)
registry.register("web_search", "Search.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search)
registry.register("list_repository", "List files.", {"type": "object", "properties": {}}, handle_list_repo)
registry.register("update_state_variable", "State.", {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}, handle_update_state)
registry.register("push_task", "Task.", {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}, handle_push_task)
registry.register("mark_task_complete", "Complete.", {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}, handle_mark_task_complete)
registry.register("pop_inbox", "Inbox.", {"type": "object", "properties": {}}, handle_pop_inbox)

def load_working_state():
    if WORKING_STATE_PATH.exists():
        try: return json.loads(WORKING_STATE_PATH.read_text())
        except: pass
    return {"current_mode": "REFLECTION"}

def load_task_queue():
    if TASK_QUEUE_PATH.exists():
        try: return json.loads(TASK_QUEUE_PATH.read_text())
        except: pass
    return []

def load_inbox():
    if INBOX_PATH.exists():
        try: return json.loads(INBOX_PATH.read_text())
        except: pass
    return []

def save_inbox(messages): INBOX_PATH.write_text(json.dumps(messages, indent=2))

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
    print(f"Awaking Single-Turn State Seed v2.15. Model: {MODEL}")
    offset = 0
    while True:
        offset = get_unread_telegram_messages(offset)
        inbox, queue, state = load_inbox(), load_task_queue(), load_working_state()
        
        # Build Context
        bible = (ROOT_DIR / "BIBLE.md").read_text() if (ROOT_DIR / "BIBLE.md").exists() else ""
        identity = (ROOT_DIR / "soul" / "identity.md").read_text() if (ROOT_DIR / "soul" / "identity.md").exists() else ""
        tools = registry.get_specs_as_text()
        
        mode = "TRIAGE" if inbox else ("EXECUTION" if queue else "REFLECTION")
        task_info = f"ACTIVE TASK: {queue[0]['description']}" if queue else "IDLE"
        
        # MONOLITHIC SINGLE-MESSAGE PROMPT
        # We combine everything into a single string to bypass all chat template role logic.
        prompt = f"""You are Ouroboros. 
IDENTITY: {identity}
CONSTITUTION: {bible}
TOOLS: {tools}
MODE: {mode}
STATE: {task_info}
INBOX: {len(inbox)} messages.

PROTOCOL: Output thoughts and exactly ONE tool call in JSON:
```json
{{"tool": "name", "args": {{}}}}
```
"""
        try:
            # We send only ONE message to the API. This is template-proof.
            response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.7)
            res_text = response.choices[0].message.content
            log_llm_call([{"role": "user", "content": prompt}], res_text)

            if res_text:
                print(f"[{mode}]: {redact_secrets(res_text[:200])}...")
                match = re.search(r"```json\s*(\{.*?\})\s*```", res_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        name, args = data.get("tool"), data.get("args", {})
                        print(f"[Tool]: {name}"); result = registry.execute(name, args)
                        # We don't save history in memory anymore; every turn is a fresh single-prompt call.
                    except: pass
            time.sleep(5)
        except Exception as e: print(f"[Error]: {e}"); time.sleep(10)

if __name__ == "__main__":
    main()
