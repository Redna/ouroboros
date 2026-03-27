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

def auto_compact_task_log(task_id: str, max_lines: int = 100) -> None:
    """Prunes the task log if it exceeds max_lines, keeping only the most recent context."""
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    if len(lines) > max_lines:
        print(f"[System] Compacting log for {task_id} ({len(lines)} lines -> {max_lines})")
        # Keep the first message (usually the task start) and the last max_lines-1
        compacted = [lines[0]] + lines[-(max_lines-1):]
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(compacted)

def wipe_global_trunk_log() -> None:
    """Explicitly clears the global trunk log to maintain 'Trunk Amnesia' during context switches."""
    log_path = constants.MEMORY_DIR / "task_log_global_trunk.jsonl"
    if log_path.exists():
        log_path.unlink()
    print("[System] Global Trunk log wiped (Amnesia protocol).")

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
    
    # Check Task Token Hard Limit
    if not is_trunk and queue:
        queue[0]["task_tokens"] = queue[0].get("task_tokens", 0) + t_count
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        if queue[0]["task_tokens"] >= int(constants.CONTEXT_WINDOW * 1.5):
            return True # Signal main loop to abort
            
    return False

def enforce_context_limits(state: Dict[str, Any], queue: List[Dict[str, Any]], task_id: str, is_trunk: bool) -> List[Dict[str, Any]]:
    """Prunes history or handles context-aware yield triggers."""
    if is_trunk or not queue:
        return queue
        
    queue[0]["turn_count"] = queue[0].get("turn_count", 0) + 1
    current_context_size = state.get("last_context_size", 0)
    max_physical_context = int(constants.CONTEXT_WINDOW * constants.CONTEXT_SAFETY_MARGIN)
    
    if queue[0]["turn_count"] >= constants.TURN_LIMIT or current_context_size > max_physical_context:
        trigger_reason = f"{constants.TURN_LIMIT}-turn limit" if queue[0]["turn_count"] >= constants.TURN_LIMIT else f"physical context exhaustion ({current_context_size}/{constants.CONTEXT_WINDOW})"
        append_task_message(task_id, {"role": "user", "content": f"[SYSTEM OVERRIDE]: Hit {trigger_reason}. You MUST use `push_task` to break your remaining work down into a new subtask immediately."})
        queue[0]["turn_count"] = 0 
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        
    return queue
