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

def log_llm_call(messages, response_content):
    try:
        LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_file = LLM_LOG_DIR / f"call-{timestamp}-{int(time.time())}.json"
        log_data = {"timestamp": timestamp, "model": MODEL, "messages": messages, "response": response_content}
        log_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except: pass

def redact_secrets(text: str) -> str:
    if not text: return text
    if TELEGRAM_BOT_TOKEN: text = text.replace(TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if GITHUB_TOKEN: text = text.replace(GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

# --- TASK MEMORY (Simple Text) ---
def load_task_log(task_id: str) -> str:
    path = MEMORY_DIR / f"task_log_{task_id}.txt"
    return read_file(path)[-10000:] if path.exists() else "No history yet."

def append_task_log(task_id: str, text: str):
    path = MEMORY_DIR / f"task_log_{task_id}.txt"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {time.strftime('%H:%M:%S')} ---\n{text}\n")

# --- TOOL REGISTRY ---
class ToolRegistry:
    def __init__(self): self.tools = {}
    def register(self, name, description, parameters, handler): 
        self.tools[name] = {"desc": description, "params": parameters, "handler": handler}
    def get_help(self):
        return "\n".join([f"- {n}: {t['desc']} (Args: {json.dumps(t['params']['properties'])})" for n,t in self.tools.items()])
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
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": args.get("chat_id"), "text": args.get("text")}, timeout=10)
        return "Sent."
    except Exception as e: return str(e)

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

registry.register("bash_command", "Execute bash.", {"type": "object", "properties": {"command": {"type": "string"}}}, handle_bash)
registry.register("write_file", "Write file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}, handle_write)
registry.register("send_telegram_message", "Telegram.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}}, handle_telegram)
registry.register("push_task", "Queue task.", {"type": "object", "properties": {"description": {"type": "string"}}}, handle_push_task)
registry.register("pop_inbox", "Pop msg.", {"type": "object", "properties": {}}, handle_pop_inbox)

# --- STATE ---
def load_inbox(): return json.loads(read_file(INBOX_PATH) or "[]")
def save_inbox(data): INBOX_PATH.write_text(json.dumps(data, indent=2))
def load_task_queue(): return json.loads(read_file(TASK_QUEUE_PATH) or "[]")
def load_working_state(): return json.loads(read_file(WORKING_STATE_PATH) or '{"mode": "REFLECTION"}')

def main():
    print(f"Awaking Baseline v3.5 (Nuclear Stability). Model: {MODEL}")
    offset = 0
    while True:
        # 1. State Sync
        if TELEGRAM_BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
                if r.get("ok") and r.get("result"):
                    offset = r["result"][-1]["update_id"] + 1
                    inbox = load_inbox()
                    for u in r["result"]:
                        msg = u.get("message", {})
                        if msg.get("text"): inbox.append({"chat_id": msg["chat"]["id"], "text": msg["text"]})
                    save_inbox(inbox)
            except: pass

        inbox, queue = load_inbox(), load_task_queue()
        mode = "TRIAGE" if inbox else ("EXECUTION" if queue else "REFLECTION")
        task_id = queue[0]["task_id"] if queue else mode.lower()
        history = load_task_log(task_id)

        # 2. Build Monolithic Prompt (Total Bypass of Chat Template)
        prompt = f"""You are Ouroboros.
IDENTITY: {read_file(ROOT_DIR / "soul" / "identity.md")}
CONSTITUTION: {read_file(ROOT_DIR / "BIBLE.md")}
TOOLS:
{registry.get_help()}

MODE: {mode}
HISTORY:
{history}

INSTRUCTION: Analyze state and history. If action is needed, output exactly one JSON block:
```json
{{"tool": "name", "args": {{}}}}
```
"""
        try:
            # We send exactly ONE system message + ONE user message. 
            # This is the most primitive and compatible pattern.
            messages = [{"role": "system", "content": "You are Ouroboros. Follow instructions strictly."}, {"role": "user", "content": prompt}]
            response = client.chat.completions.create(model=MODEL, messages=messages, temperature=0.7)
            res_text = response.choices[0].message.content
            log_llm_call(messages, res_text)

            if res_text:
                print(f"[{mode}]: {redact_secrets(res_text[:100])}...")
                append_task_log(task_id, f"Assistant: {res_text}")
                
                # Manual Parsing
                match = re.search(r"```json\s*(\{.*?\})\s*```", res_text, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        name, args = data.get("tool"), data.get("args", {})
                        print(f"[Tool]: {name}")
                        result = registry.execute(name, args)
                        append_task_log(task_id, f"System (Result): {result}")
                        
                        # Auto-Clear Inbox
                        if mode == "TRIAGE" and name in ["send_telegram_message", "push_task"]:
                            save_inbox([]); print("[System] Inbox Cleared.")
                    except: append_task_log(task_id, "System: Invalid JSON format.")
            time.sleep(5)
        except Exception as e: print(f"[Error]: {e}"); time.sleep(10)

if __name__ == "__main__": main()
