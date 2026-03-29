import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

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
            
    # Initialize structured memory store
    if not constants.MEMORY_STORE_PATH.exists():
        default_memory = {"max_entries": constants.MEMORY_MAX_ENTRIES, "last_synthesis": "", "entries": {}}
        constants.MEMORY_STORE_PATH.write_text(json.dumps(default_memory, indent=2), encoding="utf-8")

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
    """Loads and normalizes message history for a task. Includes OOM protection."""
    if not task_id: return []
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    
    # OOM Protection: If log is > 50MB, force emergency compaction before loading
    if log_path.exists() and log_path.stat().st_size > 50 * 1024 * 1024:
        print(f"[System] CRITICAL: Log {task_id} too large ({log_path.stat().st_size / 1024 / 1024:.1f}MB). Compacting...")
        emergency_compact_log(task_id)

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
    
    return raw_messages

def append_task_message(task_id: str, message_dict: Dict[str, Any]) -> None:
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    
    # Safety: Refuse to append if file is already dangerously large
    if log_path.exists() and log_path.stat().st_size > 100 * 1024 * 1024:
        print(f"[System] ERROR: Refusing to append to {task_id}. File size exceeds 100MB limit.")
        return

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

# --- Structured Memory Store CRUD ---

def _load_memory_store() -> Dict[str, Any]:
    """Loads the full memory store from disk."""
    if not constants.MEMORY_STORE_PATH.exists():
        return {"max_entries": constants.MEMORY_MAX_ENTRIES, "last_synthesis": "", "entries": {}}
    try:
        return json.loads(constants.MEMORY_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"max_entries": constants.MEMORY_MAX_ENTRIES, "last_synthesis": "", "entries": {}}

def _save_memory_store(store: Dict[str, Any]) -> None:
    """Writes the full memory store to disk."""
    constants.MEMORY_STORE_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")

def load_memory_index() -> List[str]:
    """Returns the list of memory keys for context injection."""
    store = _load_memory_store()
    return list(store.get("entries", {}).keys())

def load_memory_entry(key: str) -> str:
    """Returns the value for a memory key. Tries exact match, then substring."""
    store = _load_memory_store()
    entries = store.get("entries", {})
    if key in entries:
        return entries[key]
    # Substring fallback
    for k, v in entries.items():
        if key.lower() in k.lower():
            return f"[Matched key: {k}]\n{v}"
    return ""

def store_memory_entry(key: str, content: str) -> str:
    """Add or update a memory entry. Enforces max_entries cap."""
    store = _load_memory_store()
    entries = store.get("entries", {})
    max_entries = store.get("max_entries", constants.MEMORY_MAX_ENTRIES)

    if key not in entries and len(entries) >= max_entries:
        return f"Error: Memory full ({len(entries)}/{max_entries}). Use `forget_memory` to free a slot or merge entries first."

    is_update = key in entries
    entries[key] = content
    store["entries"] = entries
    store["last_synthesis"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_memory_store(store)
    action = "Updated" if is_update else "Stored"
    return f"{action} memory: '{key}' ({len(entries)}/{max_entries} slots used)."

def forget_memory_entry(key: str) -> str:
    """Remove a memory entry by key."""
    store = _load_memory_store()
    entries = store.get("entries", {})
    if key not in entries:
        return f"Error: No memory with key '{key}'. Available keys: {list(entries.keys())[:10]}"
    del entries[key]
    store["entries"] = entries
    _save_memory_store(store)
    max_entries = store.get("max_entries", constants.MEMORY_MAX_ENTRIES)
    return f"Forgotten: '{key}'. ({len(entries)}/{max_entries} slots used)."

def append_task_archive(task_id: str, summary: str) -> None:
    """Append a task completion record to the archive (never auto-loaded into context)."""
    record = {"task_id": task_id, "summary": summary, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(constants.TASK_ARCHIVE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

def emergency_compact_log(task_id: str, max_lines: int = 150) -> None:
    """
    LAST RESORT safety net. Only triggers if the agent ignores warnings and
    is about to crash the context window.
    """
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if len(lines) <= max_lines: return

    try:
        messages = [json.loads(line) for line in lines if line.strip()]

        # Preserve the task start (usually message 0)
        first_msg = messages[0]

        # Safely find a cutoff for the most recent messages (e.g., last 20)
        cutoff = len(messages) - 20
        while cutoff < len(messages) and messages[cutoff].get("role") != "user":
            cutoff += 1  # Ensure we cut at a user message to avoid breaking tool chains

        if cutoff >= len(messages):
            cutoff = len(messages) - 10  # Fallback

        emergency_notice = {
            "role": "user",
            "content": "[SYSTEM OVERRIDE]: Emergency compaction triggered. You failed to compress your memory in time. Middle history has been wiped to prevent a crash."
        }

        compacted = [first_msg, emergency_notice] + messages[cutoff:]

        print(f"[System] Emergency compaction for {task_id} ({len(lines)} lines -> {len(compacted)})")
        with open(log_path, "w", encoding="utf-8") as f:
            for msg in compacted:
                f.write(json.dumps(msg) + "\n")
    except Exception as e:
        print(f"[System] Error in emergency compaction {task_id}: {e}")

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
    
    # Check Task Token Hard Limit (Now applies to Trunk tasks too)
    if queue:
        queue[0]["task_tokens"] = queue[0].get("task_tokens", 0) + t_count
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        
        # Hard abort threshold (150% of context window)
        if queue[0]["task_tokens"] >= int(constants.CONTEXT_WINDOW * 1.5):
            return True # Signal main loop to abort
            
    return False

def enforce_context_limits(state: Dict[str, Any], queue: List[Dict[str, Any]], task_id: str, is_trunk: bool) -> Tuple[List[Dict[str, Any]], str]:
    """Three-tier sawtooth safety net: NORMAL, LAST_GASP, BREACH."""
    if not queue:
        return queue, "NORMAL"

    queue[0]["turn_count"] = queue[0].get("turn_count", 0) + 1
    current_context_size = state.get("last_context_size", 0)
    turn_count = queue[0]["turn_count"]

    # Thresholds
    warning_threshold = int(constants.CONTEXT_WINDOW * 0.8)
    last_gasp_threshold = int(constants.CONTEXT_WINDOW * 0.95)
    breach_threshold = int(constants.CONTEXT_WINDOW * 0.98)

    # TRUNK SAFETY
    if is_trunk:
        if turn_count > 50 or current_context_size >= breach_threshold:
            return queue, "BREACH"
        if turn_count > 45 or current_context_size >= last_gasp_threshold:
            return queue, "LAST_GASP"
        return queue, "NORMAL"

    # BRANCH SAFETY
    hit_turn_breach = turn_count > constants.TURN_LIMIT
    hit_turn_gasp = turn_count >= constants.TURN_LIMIT
    
    hit_token_breach = current_context_size >= breach_threshold
    hit_token_gasp = current_context_size >= last_gasp_threshold

    if hit_token_breach or hit_turn_breach:
        return queue, "BREACH"
    
    if hit_token_gasp or hit_turn_gasp:
        return queue, "LAST_GASP"

    return queue, "NORMAL"
