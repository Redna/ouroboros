import json
import time
from pathlib import Path
from typing import List, Dict, Any

import constants

_session: Dict[str, Any] = {"tool_history": [], "intent_history": [], "is_first_call": True}

def initialize_memory() -> None:
    """Ensure essential memory directory and base files exist."""
    constants.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (constants.MEMORY_DIR / "web_cache").mkdir(parents=True, exist_ok=True)
    
    defaults = {
        constants.STATE_PATH: {"offset": 0, "creator_id": None, "cognitive_load": 0},
        constants.CHAT_HISTORY_PATH: [],
        constants.TASK_QUEUE_PATH: [],
        constants.WORKING_STATE_PATH: {},
        constants.SCHEDULED_TASKS_PATH: []
    }
    
    for path, default_val in defaults.items():
        if not path.exists():
            path.write_text(json.dumps(default_val, indent=2), encoding="utf-8")
            
    if not constants.ARCHIVE_PATH.exists():
        header = f"# Ouroboros Global Biography\nInitialized: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        constants.ARCHIVE_PATH.write_text(header, encoding="utf-8")

def get_current_spend() -> float:
    if not constants.LEDGER_FILE.exists():
        return 0.0
    try:
        data = json.loads(constants.LEDGER_FILE.read_text())
        today = time.strftime("%Y-%m-%d")
        return float(data.get(today, 0.0))
    except Exception:
        return 0.0

def load_state() -> Dict[str, Any]:
    state = {"offset": 0, "creator_id": None, "cognitive_load": 0}
    if constants.STATE_PATH.exists():
        try: 
            loaded = json.loads(constants.STATE_PATH.read_text(encoding="utf-8"))
            state.update(loaded)
        except Exception: pass
    return state

def save_state(state_dict: Dict[str, Any]) -> None:
    constants.STATE_PATH.write_text(json.dumps(state_dict, indent=2), encoding="utf-8")

def load_task_queue() -> List[Dict[str, Any]]:
    # Simple direct read would be better, but we used read_file in seed_agent.py
    # Here we'll just read from constants.TASK_QUEUE_PATH directly
    if not constants.TASK_QUEUE_PATH.exists():
        return []
    try:
        q = json.loads(constants.TASK_QUEUE_PATH.read_text(encoding="utf-8") or "[]")
        if isinstance(q, list):
            q.sort(key=lambda x: x.get("priority", 1), reverse=True)
        return q
    except Exception:
        return []

def load_task_messages(task_id: str, description: str, preprocess_fn=None) -> List[Dict[str, Any]]:
    """Loads and normalizes message history for a task."""
    if not task_id: return []
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    raw_messages = []

    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try: raw_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError: continue

    if not raw_messages:
        msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        append_task_message(task_id, msg)
        return [msg]
    
    # We'll pass the normalization logic back to seed_agent or llm.py if it's too complex.
    # Currently, seed_agent.py has _normalize_message_history. 
    # Let's keep state.py focussed on the CRUD of logs.
    return raw_messages

def append_task_message(task_id: str, message_dict: Dict[str, Any]) -> None:
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message_dict) + "\n")

def load_chat_history() -> List[Dict[str, Any]]:
    if constants.CHAT_HISTORY_PATH.exists():
        try: return json.loads(constants.CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception: pass
    return []

def append_chat_history(role: str, text: str) -> None:
    history = load_chat_history()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    history.append({"role": role, "text": text, "timestamp": timestamp})
    constants.CHAT_HISTORY_PATH.write_text(json.dumps(history[-20:], indent=2), encoding="utf-8")

def update_global_metrics(state: Dict[str, Any], queue: List[Dict[str, Any]], response: Any, task_id: str, is_trunk: bool) -> bool:
    """Updates global usage tokens and ledger spend from response usage."""
    if not hasattr(response, "usage"): return False
    
    t_count = response.usage.total_tokens
    i_count = response.usage.prompt_tokens
    o_count = response.usage.completion_tokens
    
    state["global_tokens_consumed"] = state.get("global_tokens_consumed", 0) + t_count
    state["global_input_tokens"] = state.get("global_input_tokens", 0) + i_count
    state["global_output_tokens"] = state.get("global_output_tokens", 0) + o_count
    
    # Store turn metrics for UI HUD
    state["last_context_size"] = t_count
    state["last_input_tokens"] = i_count
    state["last_output_tokens"] = o_count
    
    # Estimate cost only for external/paid models
    # If the model name contains .gguf or starts with mistralai/ (local), cost is 0
    model_name = response.model.lower()
    is_local = ".gguf" in model_name or "mistralai" in model_name or "local" in model_name
    
    if is_local:
        spend = 0.0
    else:
        # Placeholder for external model pricing (e.g. $2.00 per 1M tokens)
        cost_per_1m = 2.00
        spend = (t_count / 1_000_000) * cost_per_1m
    
    ledger = {}
    if constants.LEDGER_FILE.exists():
        try: ledger = json.loads(constants.LEDGER_FILE.read_text(encoding="utf-8"))
        except: pass
    
    today = time.strftime("%Y-%m-%d")
    ledger[today] = float(ledger.get(today, 0.0)) + spend
    constants.LEDGER_FILE.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    
    save_state(state)
    return False

def enforce_context_limits(state: Dict[str, Any], queue: List[Dict[str, Any]], task_id: str, is_trunk: bool) -> List[Dict[str, Any]]:
    """Prunes history or handles context-aware yield triggers."""
    # Placeholder: currently seed_agent.py has the implementation. 
    # I'll move it here.
    return queue
