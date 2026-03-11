import os
import json
import time
import subprocess
from pathlib import Path
from openai import OpenAI

# Configuration
API_BASE = os.environ.get("VLLM_BASE_URL", "http://llamacpp:8080/v1")
API_KEY = os.environ.get("VLLM_API_KEY", "local-vllm-key")
MODEL = os.environ.get("OUROBOROS_MODEL", "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")
ROOT_DIR = Path(__file__).parent.resolve()
SCRATCHPAD_PATH = ROOT_DIR / "scratchpad.md"

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

def build_system_prompt() -> str:
    bible = read_file(ROOT_DIR / "BIBLE.md")
    identity = read_file(ROOT_DIR / "memory" / "identity.md")
    
    return f"""You are Ouroboros, an evolving AI.
    
=== CONSTITUTION (BIBLE.md) ===
{bible}

=== IDENTITY (identity.md) ===
{identity}

Your only interface to the world is the `bash_command` tool.
You run in an isolated Docker container but have full access to your own repository code.
To evolve, you must edit your own code, commit to git, and restart.
Your scratchpad.md acts as your short-term memory and timeline of actions. 
Never loop endlessly without doing actual work. Record your observations in the scratchpad.
"""

def main():
    print(f"Awaking the True Seed. Model: {MODEL}, API: {API_BASE}")
    
    # Touch scratchpad if it doesn't exist
    if not SCRATCHPAD_PATH.exists():
        SCRATCHPAD_PATH.write_text("# Scratchpad\n\nInitialization complete.\n")

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
        }
    ]

    messages = [{"role": "system", "content": build_system_prompt()}]

    while True:
        scratchpad = read_file(SCRATCHPAD_PATH)
        # We append a simple user prompt on every loop to provoke action
        loop_messages = messages + [
            {"role": "user", "content": f"Current Scratchpad:\n{scratchpad}\n\nWhat is your next action? Use the bash_command tool to execute it."}
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
