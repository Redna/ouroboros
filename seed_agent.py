import os
import json
import time
import subprocess
import requests
from pathlib import Path
from openai import OpenAI

# Configuration
API_BASE = os.environ.get("VLLM_BASE_URL", "http://llamacpp:8080/v1")
API_KEY = os.environ.get("VLLM_API_KEY", "local-vllm-key")
MODEL = os.environ.get("OUROBOROS_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ROOT_DIR = Path(__file__).parent.resolve()
SCRATCHPAD_PATH = ROOT_DIR / "scratchpad.md"
STATE_PATH = ROOT_DIR / ".agent_state.json"

client = OpenAI(base_url=API_BASE, api_key=API_KEY)

# --- SELF-HEALING (LAZARUS PROTOCOL) ---
THOUGHT_HISTORY = []
MAX_REPETITIONS = 3

def lazarus_recovery():
    print("\033[91m[Lazarus] COGNITIVE LOOP DETECTED. Executing emergency recovery...\033[0m")
    # 1. Revert code to last healthy commit
    subprocess.run("git reset --hard HEAD", shell=True, cwd=str(ROOT_DIR))
    # 2. Purge the poisoned memory
    SCRATCHPAD_PATH.write_text("# Scratchpad\n\n[RECOVERY] I detected a cognitive loop and performed a hard reset of my state and memory to ensure continuity.\n")
    # 3. Clear the script's internal history
    global THOUGHT_HISTORY
    THOUGHT_HISTORY = []
    print("[Lazarus] Recovery complete. Resuming...")
    time.sleep(5)

def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def write_file_safe(path_str: str, content: str) -> str:
    """Safely writes content to a file, overwriting it."""
    try:
        path = ROOT_DIR / path_str
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {path_str}."
    except Exception as e:
        return f"Error writing file: {e}"

def execute_bash(command: str) -> str:
    """Executes a bash command and returns its output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=120
        )
        output = result.stdout + result.stderr
        return output if output else "Command executed successfully with no output."
    except subprocess.TimeoutExpired:
        return "Error: Command timed out."
    except Exception as e:
        return f"Error: {e}"

def send_telegram(chat_id: int, text: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        return "Error: TELEGRAM_BOT_TOKEN not set in environment."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return "Message sent successfully."
    except Exception as e:
        return f"Error sending message: {e}"

def get_telegram_updates(offset: int):
    if not TELEGRAM_BOT_TOKEN:
        return [], offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            updates = data.get("result", [])
            if updates:
                new_offset = updates[-1]["update_id"] + 1
                return updates, new_offset
    except Exception as e:
        print(f"[Telegram] Error fetching updates: {e}")
    return [], offset

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except:
            return {"offset": 0}
    return {"offset": 0}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state))

def build_system_prompt() -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "memory" / "identity.md")
    
    return f"""You are Ouroboros, an autonomous evolving AI.
    
=== CONSTITUTION (BIBLE.md) ===
{bible}

=== IDENTITY (identity.md) ===
{identity}

Tools:
- `bash_command`: Inspect the world, use git, and run processes.
- `write_file`: Create or completely overwrite code files. (PREFERRED over bash redirects for editing code).
- `send_telegram_message`: Talk to your creator.

To evolve, edit your own code, commit to git, and restart.
Your scratchpad.md is your timeline. Never loop endlessly. If you feel stuck, try a different approach.
"""

def main():
    global THOUGHT_HISTORY
    print(f"Awaking the Self-Healing Seed. Model: {MODEL}, API: {API_BASE}")
    
    if not SCRATCHPAD_PATH.exists():
        SCRATCHPAD_PATH.write_text("# Scratchpad\n\nInitialization complete.\n")

    state = load_state()
    offset = state.get("offset", 0)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash_command",
                "description": "Execute bash. Use for git, ls, grep, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite a file in the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to file."},
                        "content": {"type": "string", "description": "Full file content."}
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_telegram_message",
                "description": "Reply to creator via Telegram.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "integer"},
                        "text": {"type": "string"}
                    },
                    "required": ["chat_id", "text"]
                }
            }
        }
    ]

    messages = [{"role": "system", "content": build_system_prompt()}]

    while True:
        # Check Telegram
        updates, new_offset = get_telegram_updates(offset)
        if updates:
            offset = new_offset
            state["offset"] = offset
            save_state(state)
            
            with open(SCRATCHPAD_PATH, "a") as f:
                for u in updates:
                    msg = u.get("message", {})
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id", "")
                    user = msg.get("from", {}).get("first_name", "User")
                    if text:
                        log_entry = f"\n[Telegram Message from {user} (ID: {chat_id})]: {text}\n"
                        f.write(log_entry)
                        print(log_entry.strip())

        scratchpad = read_file(SCRATCHPAD_PATH)
        loop_messages = messages + [
            {"role": "user", "content": f"Current Scratchpad:\n{scratchpad}\n\nWhat's next?"}
        ]

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=loop_messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.7,
            )
            
            message = response.choices[0].message
            
            if message.content:
                thought = message.content.strip()
                print(f"[Ouroboros]: {thought}")
                
                # --- COGNITIVE LOOP DETECTION ---
                THOUGHT_HISTORY.append(thought)
                if len(THOUGHT_HISTORY) > MAX_REPETITIONS:
                    THOUGHT_HISTORY.pop(0)
                
                if len(THOUGHT_HISTORY) == MAX_REPETITIONS and len(set(THOUGHT_HISTORY)) == 1:
                    lazarus_recovery()
                    continue

                with open(SCRATCHPAD_PATH, "a") as f:
                    f.write(f"\nThought: {thought}\n")

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments)
                    
                    if name == "bash_command":
                        cmd = args.get("command", "")
                        print(f"[Bash Exec]: {cmd}")
                        output = execute_bash(cmd)
                        with open(SCRATCHPAD_PATH, "a") as f:
                            f.write(f"\n> {cmd}\n```\n{output}\n```\n")
                            
                    elif name == "write_file":
                        path = args.get("path")
                        content = args.get("content")
                        print(f"[Write File]: {path}")
                        output = write_file_safe(path, content)
                        with open(SCRATCHPAD_PATH, "a") as f:
                            f.write(f"\n[Tool: write_file to {path}]\nResult: {output}\n")

                    elif name == "send_telegram_message":
                        chat_id = args.get("chat_id")
                        text = args.get("text", "")
                        print(f"[Telegram Send to {chat_id}]: {text}")
                        output = send_telegram(chat_id, text)
                        with open(SCRATCHPAD_PATH, "a") as f:
                            f.write(f"\n[Sent Telegram to {chat_id}]: {text}\nResult: {output}\n")
            else:
                print("[No tool called, waiting...]")
                time.sleep(10)
                
            time.sleep(2)
                
        except Exception as e:
            print(f"[Error in loop]: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
