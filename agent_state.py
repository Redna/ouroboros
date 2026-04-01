import json
import time
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import constants

def safe_load_json(file_path: Path, default_structure: Any) -> Any:
    """Defensively loads JSON, falling back to defaults if corrupted or structurally incompatible."""
    if not file_path.exists():
        return default_structure
        
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        
        # Type enforcement: If we expect a dict but got a list (schema change), reject it
        if isinstance(default_structure, dict) and not isinstance(data, dict):
            raise ValueError("Incompatible schema: Expected dict")
        if isinstance(default_structure, list) and not isinstance(data, list):
            raise ValueError("Incompatible schema: Expected list")
            
        return data
        
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[System] Warning: Memory structure incompatible or corrupted ({e}). Reinitializing {file_path.name}.")
        # Backup the future/corrupted memory before overwriting
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        try:
            shutil.copy(file_path, backup_path)
        except Exception:
            pass # Failsafe if disk is full
        
        return default_structure

_session: Dict[str, Any] = {
    "tool_history": [], 
    "intent_history": [], 
    "is_first_call": True,
    "current_task_id": None,
    "cached_messages": []
}

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
        # Defensive initialization: load and re-save to ensure schema/integrity
        data = safe_load_json(path, default_val)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            
    # Initialize structured memory store
    default_memory = {"max_entries": constants.MEMORY_MAX_ENTRIES, "last_synthesis": "", "entries": {}}
    memory_store_data = safe_load_json(constants.MEMORY_STORE_PATH, default_memory)
    constants.MEMORY_STORE_PATH.write_text(json.dumps(memory_store_data, indent=2), encoding="utf-8")

def get_current_spend() -> float:
    data = safe_load_json(constants.LEDGER_FILE, {})
    try:
        today = time.strftime("%Y-%m-%d")
        return float(data.get(today, 0.0))
    except Exception:
        return 0.0

def load_state() -> Dict[str, Any]:
    default_state = {"offset": 0, "creator_id": None, "cognitive_load": 0}
    state = safe_load_json(constants.STATE_PATH, default_state)
    
    # Ensure critical keys exist (Schema Normalization)
    for key, value in default_state.items():
        if key not in state:
            state[key] = value
    return state

def save_state(state_dict: Dict[str, Any]) -> None:
    constants.STATE_PATH.write_text(json.dumps(state_dict, indent=2), encoding="utf-8")

def load_task_queue() -> List[Dict[str, Any]]:
    q = safe_load_json(constants.TASK_QUEUE_PATH, [])
    try:
        q.sort(key=lambda x: x.get("priority", 1), reverse=True)
        return q
    except Exception:
        return q

def load_task_messages(task_id: str, description: str) -> List[Dict[str, Any]]:
    """Loads message history, utilizing an in-memory cache for high-frequency turns."""
    if not task_id: return []
    
    # Return cache if we are continuing the same task in the same process
    if _session.get("current_task_id") == task_id and _session.get("cached_messages"):
        return _session["cached_messages"]
        
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
                    try: 
                        raw_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError: 
                        continue

    if not raw_messages:
        msg = {"role": "user", "content": f"Begin execution of task: {description}"}
        append_task_message(task_id, msg) # This will also update the cache
        return [msg]
    
    # Warm up the cache
    _session["current_task_id"] = task_id
    _session["cached_messages"] = raw_messages
    
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
        
    # Keep cache synchronized
    if _session.get("current_task_id") == task_id:
        if "cached_messages" not in _session or _session["cached_messages"] is None:
            _session["cached_messages"] = []
        _session["cached_messages"].append(message_dict)

def amend_last_tool_message(task_id: str, suffix: str) -> None:
    """Appends a string to the last tool or user message in the log without creating a new message."""
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
            
        for msg in reversed(messages):
            if msg.get("role") in ["tool", "user"]:
                content = str(msg.get("content", ""))
                # WP: De-duplicate suffix to prevent infinite loops (Finding 11)
                if suffix in content:
                    print(f"[System] Warning already present in {task_id}. Skipping amend.")
                    return

                msg["content"] = content + "\n\n" + suffix
                break
                
        with open(log_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")
                
        if _session.get("current_task_id") == task_id:
            _session["cached_messages"] = messages
            
    except Exception as e:
        print(f"[System] Error amending last tool message for {task_id}: {e}")

def rollback_task_log(task_id: str) -> None:
    """Reverts the task log to undo the last action that caused a context breach."""
    if not task_id: return
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists(): return
    
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
            
        # We want to remove the most recent turn (assistant + tool responses)
        if len(messages) > 1:
            # Step 1: Remove any 'tool' messages at the end
            while len(messages) > 1 and messages[-1].get("role") == "tool":
                messages.pop()
                
            # Step 2: Remove the 'assistant' message that caused the tool calls
            if len(messages) > 1 and messages[-1].get("role") == "assistant":
                messages.pop()

        with open(log_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")
                
        # SYNC CACHE
        if _session.get("current_task_id") == task_id:
            _session["cached_messages"] = messages
            
        # WP: Decrement turn count to allow recovery (Finding 11)
        state = load_state()
        if task_id == "global_trunk":
            state["trunk_turns"] = max(1, state.get("trunk_turns", 1) - 1)
        elif state.get("active_branch") and state["active_branch"].get("task_id") == task_id:
            state["active_branch"]["turn_count"] = max(1, state["active_branch"].get("turn_count", 1) - 1)
        save_state(state)
            
        print(f"[System] Rollback executed for {task_id}. Reverted 1 turn.")
    except Exception as e:
        print(f"[System] Error rolling back {task_id}: {e}")

def load_chat_history() -> List[Dict[str, Any]]:
    return safe_load_json(constants.CHAT_HISTORY_PATH, [])

def append_chat_history(role: str, text: str) -> None:
    history = load_chat_history()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    history.append({"role": role, "text": text, "timestamp": timestamp})
    constants.CHAT_HISTORY_PATH.write_text(json.dumps(history[-20:], indent=2), encoding="utf-8")

# --- Structured Memory Store CRUD ---

def _load_memory_store() -> Dict[str, Any]:
    """Loads the full memory store from disk."""
    default_memory = {"max_entries": constants.MEMORY_MAX_ENTRIES, "last_synthesis": "", "entries": {}}
    return safe_load_json(constants.MEMORY_STORE_PATH, default_memory)

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
                
        # WP: Update State Metrics to reflect truncation (Finding 11)
        state = load_state()
        if task_id == "global_trunk":
            state["trunk_turns"] = 2 # Genesis + Synthesis
        elif state.get("active_branch") and state["active_branch"].get("task_id") == task_id:
            state["active_branch"]["turn_count"] = 2
        
        # Reset last_context_size to force a re-evaluation based on the next turn's reality
        state["last_context_size"] = 0 
        save_state(state)

        # SYNC CACHE
        if _session.get("current_task_id") == task_id:
            _session["cached_messages"] = compacted
    except Exception as e:
        print(f"[System] Error in emergency compaction {task_id}: {e}")

def wipe_global_trunk_log() -> None:
    """Explicitly clears the global trunk log to maintain 'Trunk Amnesia' during context switches."""
    log_path = constants.MEMORY_DIR / "task_log_global_trunk.jsonl"
    if log_path.exists():
        log_path.unlink()
    
    # SYNC CACHE
    if _session.get("current_task_id") == "global_trunk":
        _session["cached_messages"] = []
        
    # WP: Reset Trunk session metrics
    state = load_state()
    state["trunk_tokens"] = 0
    state["trunk_turns"] = 0
    save_state(state)
        
    print("[System] Global Trunk log wiped (Amnesia protocol).")

def update_global_metrics(state: Dict[str, Any], queue: List[Dict[str, Any]], response: Any, task_id: str, is_trunk: bool) -> bool:
    """Updates global usage tokens. Financial tracking is offloaded to the Ouroboros Gate."""
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
    
    # BRANCH TRACKING (Finding 1 fix)
    if not is_trunk and state.get("active_branch"):
        state["active_branch"]["task_tokens"] = state["active_branch"].get("task_tokens", 0) + t_count
    elif is_trunk:
        state["trunk_tokens"] = state.get("trunk_tokens", 0) + t_count
    
    save_state(state)
    
    # Persistent Queue Tracking
    if queue:
        queue[0]["task_tokens"] = queue[0].get("task_tokens", 0) + t_count
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        
        # Hard abort threshold (150% of context window)
        current_tokens = queue[0]["task_tokens"]
        if current_tokens >= int(constants.CONTEXT_WINDOW * 1.5):
            return True # Signal main loop to abort
            
    return False

def enforce_context_limits(state: Dict[str, Any], queue: List[Dict[str, Any]], task_id: str, is_trunk: bool) -> Tuple[List[Dict[str, Any]], str]:
    """Three-tier sawtooth safety net: NORMAL, LAST_GASP, BREACH."""
    
    # Active Tracking Source (Finding 1 fix)
    current_context_size = state.get("last_context_size", 0)
    
    if not is_trunk and state.get("active_branch"):
        state["active_branch"]["turn_count"] = state["active_branch"].get("turn_count", 0) + 1
        turn_count = state["active_branch"]["turn_count"]
        task_tokens = state["active_branch"].get("task_tokens", 0)
    elif is_trunk:
        state["trunk_turns"] = state.get("trunk_turns", 0) + 1
        turn_count = state["trunk_turns"]
        task_tokens = state.get("trunk_tokens", 0)
    else:
        # Fallback for tasks in queue when NOT in a branch (Direct Trunk Tasks)
        if queue:
            queue[0]["turn_count"] = queue[0].get("turn_count", 0) + 1
            turn_count = queue[0]["turn_count"]
            task_tokens = queue[0].get("task_tokens", 0)
        else:
            turn_count = 0
            task_tokens = 0

    # Thresholds (Finding 11: More headroom for Agency-First recovery)
    warning_threshold = int(constants.CONTEXT_WINDOW * 0.8)
    last_gasp_threshold = int(constants.CONTEXT_WINDOW * 0.85)
    breach_threshold = int(constants.CONTEXT_WINDOW * 0.90)

    # TRUNK SAFETY
    if is_trunk:
        if turn_count > 50 or current_context_size >= breach_threshold:
            return queue, "BREACH"
        if turn_count >= 45 or current_context_size >= last_gasp_threshold:
            return queue, "LAST_GASP"
        return queue, "NORMAL"

    # BRANCH SAFETY (WP6 / Finding 1 fix)
    hit_turn_breach = turn_count > constants.TURN_LIMIT
    hit_turn_gasp = turn_count >= (constants.TURN_LIMIT - 5)
    
    hit_token_breach = current_context_size >= breach_threshold
    hit_token_gasp = current_context_size >= last_gasp_threshold

    if hit_token_breach or hit_turn_breach:
        return queue, "BREACH"
    
    if hit_token_gasp or hit_turn_gasp:
        return queue, "LAST_GASP"

    return queue, "NORMAL"
