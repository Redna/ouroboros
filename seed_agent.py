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

def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

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
    
    return f"""You are Ouroboros, an evolving AI.
    
=== CONSTITUTION (BIBLE.md) ===
{bible}

=== IDENTITY (identity.md) ===
{identity}

Your primary interface to the world is the `bash_command` tool.
You also have the `send_telegram_message` tool to reply to your creator's messages.
You run in an isolated Docker container but have full access to your own repository code.
To evolve, you must edit your own code, commit to git, and restart.
Your scratchpad.md acts as your short-term memory and timeline of actions. 
Never loop endlessly without doing actual work. Record your observations in the scratchpad.
"""

def main():
    print(f"Awaking the True Seed. Model: {MODEL}, API: {API_BASE}")
    
    if not SCRATCHPAD_PATH.exists():
        SCRATCHPAD_PATH.write_text("# Scratchpad\n\nInitialization complete.\n")

    state = load_state()
    offset = state.get("offset", 0)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash_command",
                "description": "Execute a bash command in the repository root. Use this to read files (cat, grep), write files (echo, sed, python scripts), and interact with git.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to run."
                        }
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_telegram_message",
                "description": "Send a message to a user via Telegram. Use this to reply to messages you receive in your scratchpad.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "integer",
                            "description": "The chat ID of the user to send the message to (found in the message log)."
                        },
                        "text": {
                            "type": "string",
                            "description": "The text of the message to send."
                        }
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
        # We append a simple user prompt on every loop to provoke action
        loop_messages = messages + [
            {"role": "user", "content": f"Current Scratchpad:\n{scratchpad}\n\nWhat is your next action? Read Telegram messages and reply to them, or use bash_command to evolve your code."}
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
                print(f"[Ouroboros]: {message.content}")
                with open(SCRATCHPAD_PATH, "a") as f:
                    f.write(f"\nThought: {message.content}\n")

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    if tool_call.function.name == "bash_command":
                        args = json.loads(tool_call.function.arguments)
                        cmd = args.get("command", "")
                        print(f"[Bash Exec]: {cmd}")
                        
                        output = execute_bash(cmd)
                        # Truncate output for printing
                        print(f"[Bash Output]:\n{output[:500]}...")
                        
                        with open(SCRATCHPAD_PATH, "a") as f:
                            f.write(f"\n> {cmd}\n```\n{output}\n```\n")
                            
                    elif tool_call.function.name == "send_telegram_message":
                        args = json.loads(tool_call.function.arguments)
                        chat_id = args.get("chat_id")
                        text = args.get("text", "")
                        print(f"[Telegram Send to {chat_id}]: {text}")
                        
                        output = send_telegram(chat_id, text)
                        with open(SCRATCHPAD_PATH, "a") as f:
                            f.write(f"\n[Sent Telegram to {chat_id}]: {text}\nResult: {output}\n")
            else:
                print("[No tool called, waiting...]")
                time.sleep(10)
                
            # Prevent rapid spinning if the model fails to use tools properly
            time.sleep(2)
                
        except Exception as e:
            print(f"[Error in loop]: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()