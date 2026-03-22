"""
Memory Operations Module — Ouroboros v2.0.14+

Extracted memory/state management functions from seed_agent.py per P5 Minimalism.
Handles task logs, chat history, agent state, and auto-compaction logic.
"""

import json
from pathlib import Path
from typing import List, Dict, Any

# --- PATH CONSTANTS ---
MEMORY_DIR = Path("/memory")
AGENT_STATE_PATH = MEMORY_DIR / ".agent_state.json"


def shed_heavy_payloads(messages: List[Dict[str, Any]], retain_full_last_n: int = 4) -> List[Dict[str, Any]]:
    """
    Reduces token load on long message histories by stripping or abbreviating
    assistant tool_call payloads. Only the most recent N messages are kept intact.
    """
    result = []
    total_messages = len(messages)

    for idx, msg in enumerate(messages):
        remaining = total_messages - idx
        if remaining <= retain_full_last_n:
            result.append(msg)
            continue

        if msg.get("role") != "assistant":
            result.append(msg)
            continue

        tool_call = msg.get("tool_call", {})
        args_raw = tool_call.get("arguments", "{}")
        try:
            parsed = json.loads(args_raw)
            summary_keys: Dict[str, Any] = {}
            for k, v in parsed.items():
                if isinstance(v, str):
                    summary_keys[k] = f"<str:{len(v)}>"
                elif isinstance(v, (int, float)):
                    summary_keys[k] = str(v)
                else:
                    summary_keys[k] = f"<{type(v).__name__}>"
            msg = dict(msg)  # shallow copy
            msg["tool_call"] = dict(tool_call)
            msg["tool_call"]["arguments"] = json.dumps(summary_keys)
        except Exception:
            pass
        result.append(msg)

    return result


def load_task_messages(task_id: str, description: str) -> List[Dict[str, Any]]:  # type: ignore
    """
    Loads the task log for a given task_id from /memory/task_log_<task_id>.jsonl.
    Returns a list of message dicts compatible with OpenAI-style API.
    Prefixes with Turn-0 system + user messages.
    """
    path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "You are Ouroboros, a self-evolving cognitive agent."},
        {
            "role": "user",
            "content": description + "\n\nYou have access to the following tools: bash_command, read_file, write_file, patch_file, send_telegram_message, push_task, mark_task_complete, update_state_variable, web_search, fetch_webpage, compress_memory_block, search_memory_archive, store_memory_insight, clear_inbox, request_restart, hibernate"
        },
    ]

    if not path.exists():
        return messages

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                role = entry.get("role", "unknown")
                content = entry.get("content", "")

                if role == "assistant":
                    # Handle assistant messages (might include tool_call)
                    tool_calls = entry.get("tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            messages.append({
                                "role": "assistant", 
                                "content": "", 
                                "tool_calls": [
                                    {
                                        "id": str(tc["id"]), 
                                        "name": str(tc["name"]), 
                                        "arguments": str(json.dumps(tc.get("arguments", {})))
                                    }
                                ]
                            })
                    else:
                        messages.append({"role": "assistant", "content": content})
                elif role == "user":
                    # Handle tool response
                    messages.append({"role": "user", "content": content})
                else:
                    # Fallback
                    messages.append({"role": role, "content": content})
            except json.JSONDecodeError:
                continue

    return messages


def append_task_message(task_id: str, message_dict: Dict[str, Any]) -> None:
    """Appends a single JSONL message to the task log file."""
    path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")


def load_chat_history() -> List[Dict[str, Any]]:
    """Loads recent chat history from /memory/chat_history.json (last 20 messages)."""
    path = MEMORY_DIR / "chat_history.json"
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
            if isinstance(history, list):
                return history[-20:]
    except Exception:
        pass
    return []


def append_chat_history(role: str, text: str) -> None:
    """Appends a single message to /memory/chat_history.json (keeps last 20)."""
    path = MEMORY_DIR / "chat_history.json"
    history = []

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except Exception:
            history = []

    history.append({"role": role, "content": text})
    history = history[-20:]  # Keep last 20

    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def load_state() -> Dict[str, Any]:
    """Loads agent state from AGENT_STATE_PATH."""
    if not AGENT_STATE_PATH.exists():
        return {}
    
    try:
        with open(AGENT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(updates: Dict[str, Any]) -> None:
    """Updates agent state from AGENT_STATE_PATH."""
    current = load_state()
    current.update(updates)
    
    with open(AGENT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)


def auto_compact_task_log(task_id: str, max_messages: int = 40) -> None:
    """
    Reduces token load on task logs by compacting old messages.
    Keeps the last N messages in full detail, replaces older ones with a summary.
    """
    path = MEMORY_DIR / f"task_log_{task_id}.jsonl"
    
    if not path.exists():
        return
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        if len(lines) <= max_messages:
            return
        
        # Keep last N messages in full detail
        recent_lines = lines[-max_messages:]
        
        # Create a summary of older messages
        old_count = len(lines) - max_messages
        summary_line = json.dumps({
            "role": "system", 
            "content": f"[COMPACTED: {old_count} earlier messages removed for token efficiency]"
        }) + "\n"
        
        # Rewrite with compacted history
        with open(path, "w", encoding="utf-8") as f:
            f.write(summary_line)
            for line in recent_lines:
                f.write(line)
                
    except Exception:
        pass
