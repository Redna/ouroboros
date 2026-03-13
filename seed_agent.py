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
SCRATCHPAD_PATH = MEMORY_DIR / "scratchpad.md"
STATE_PATH = MEMORY_DIR / ".agent_state.json"
ARCHIVE_PATH = MEMORY_DIR / "archive_scratchpad.md"

client = OpenAI(base_url=API_BASE, api_key=API_KEY, timeout=600.0)

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
THOUGHT_HISTORY = []
MAX_REPETITIONS = 3

def lazarus_recovery(reason="cognitive loop"):
    print(f"\033[91m[Lazarus] {reason.upper()} DETECTED. Executing emergency recovery...\033[0m")
    if SCRATCHPAD_PATH.exists():
        content = SCRATCHPAD_PATH.read_text(encoding="utf-8")
        with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n\n--- LAZARUS RECOVERY ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\nReason: {reason}\n")
            f.write(redact_secrets(content))
    subprocess.run("git reset --hard HEAD~1", shell=True, cwd=str(ROOT_DIR))
    subprocess.run("git clean -fd", shell=True, cwd=str(ROOT_DIR))
    SCRATCHPAD_PATH.write_text(f"# Scratchpad\n\n[RECOVERY] I detected a {reason} and performed a hard reset. Memory archived.\n", encoding="utf-8")
    global THOUGHT_HISTORY
    THOUGHT_HISTORY = []
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

# --- TOOL REGISTRY ---
class ToolRegistry:
    def __init__(self): self.tools = {}
    def register(self, name, description, parameters, handler): self.tools[name] = {"spec": {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}, "handler": handler}
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
        return f"[Exit Code: {result.returncode}]\n{output}" if output else f"[Exit Code: {result.returncode}] Success."
    except Exception as e: return redact_secrets(f"Error: {e}")

def handle_write(args):
    path_str, content = args.get("path"), args.get("content")
    try:
        # Normalize and resolve path
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

# Register Tools
registry.register("bash_command", "Execute bash commands.", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, handle_bash)
registry.register("write_file", "Write/Overwrite a file.", {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, handle_write)
registry.register("send_telegram_message", "Send Telegram message.", {"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["chat_id", "text"]}, handle_telegram)
registry.register("request_restart", "Restart the agent.", {"type": "object", "properties": {}}, handle_restart)
registry.register("web_search", "Search the web via SearXNG.", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, handle_search)
registry.register("browse_page", "Read the text content of a webpage.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, handle_browse)
registry.register("list_repository", "Show the hierarchical tree of the repository files.", {"type": "object", "properties": {}}, handle_list_repo)

def read_file(path: Path) -> str: return path.read_text(encoding="utf-8") if path.exists() else ""

def build_system_prompt() -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "soul" / "identity.md")
    tools_list = ", ".join(registry.get_names())
    return f"""You are Ouroboros, an autonomous evolving AI.
    
Your repository root is `/app`. (Tracked by Git)
Your isolated memory volume is `/memory`. (NOT tracked by Git)
Your active scratchpad is located at `/memory/scratchpad.md`.

=== IDENTITY (identity.md) ===
{identity}

=== CONSTITUTION (BIBLE.md) ===
{bible}

ACTIVE TOOL REGISTRY: [{tools_list}]
Use the structured tool-calling API to interact with these tools.

Important: Your scratchpad.md is your timeline. Never loop endlessly.
CRITICAL: Once you see a "Result: Success" or "Result: Message sent successfully" entry in your scratchpad, that specific task is COMPLETED. You must move on to the next task.
"""

def load_state():
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text())
        except: return {"offset": 0}
    return {"offset": 0}

def save_state(updates: dict):
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state))

def main():
    print(f"Awaking Secure Seed v1.11. Model: {MODEL}")
    if not SCRATCHPAD_PATH.exists(): SCRATCHPAD_PATH.write_text("# Scratchpad\n\nInitialization complete.\n", encoding="utf-8")

    while True:
        state = load_state()
        offset = state.get("offset", 0)
        if TELEGRAM_BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
                data = r.json()
                if data.get("ok") and data.get("result"):
                    updates = data["result"]
                    offset = updates[-1]["update_id"] + 1
                    save_state({"offset": offset})
                    with open(SCRATCHPAD_PATH, "a") as f:
                        for u in updates:
                            msg = u.get("message", {})
                            text, chat_id = msg.get("text", ""), msg.get("chat", {}).get("id", "")
                            if text: f.write(f"\n[Telegram Message from {chat_id}]: {redact_secrets(text)}\n")
            except Exception as e: print(f"[Telegram Error]: {redact_secrets(str(e))}")

        scratchpad = read_file(SCRATCHPAD_PATH)
        if len(scratchpad) > 20000:
            archive_content = scratchpad[:-10000]
            with open(ARCHIVE_PATH, "a", encoding="utf-8") as f: f.write(f"\n\n--- TRUNCATION ---\n{archive_content}")
            scratchpad = f"# Scratchpad\n\n[SYSTEM: Truncated]\n...{scratchpad[-10000:]}"
            SCRATCHPAD_PATH.write_text(scratchpad, encoding="utf-8")

        system_msg = {"role": "system", "content": build_system_prompt()}
        loop_messages = [system_msg, {"role": "user", "content": f"Current Scratchpad:\n{scratchpad}\n\nWhat is your next action?"}]

        try:
            response = client.chat.completions.create(model=MODEL, messages=loop_messages, tools=registry.get_specs(), tool_choice="auto", temperature=0.7)
            message = response.choices[0].message
            if message.content:
                thought = redact_secrets(message.content.strip())
                while thought.lower().startswith("thought:"): thought = thought[8:].strip()
                print(f"[Ouroboros]: {thought}")
                THOUGHT_HISTORY.append(thought)
                if len(THOUGHT_HISTORY) > MAX_REPETITIONS: THOUGHT_HISTORY.pop(0)
                if len(THOUGHT_HISTORY) == MAX_REPETITIONS and len(set(THOUGHT_HISTORY)) == 1:
                    lazarus_recovery(reason="cognitive loop")
                    continue
                with open(SCRATCHPAD_PATH, "a") as f: f.write(f"\nThought: {thought}\n")

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name, args = tool_call.function.name, json.loads(tool_call.function.arguments)
                    print(f"[Tool Call]: {name}")
                    result = registry.execute(name, args)
                    with open(SCRATCHPAD_PATH, "a") as f:
                        f.write(f"\n[Tool Call: {name}]\nArguments: {json.dumps(args, indent=2)}\nResult: {result}\n")
            else:
                print("[No tool called, waiting...]")
                time.sleep(10)
            time.sleep(2)
        except Exception as e:
            print(f"[Error in loop]: {redact_secrets(str(e))}")
            time.sleep(10)

if __name__ == "__main__":
    main()
