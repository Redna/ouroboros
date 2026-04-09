import os
import sys
import json
import time
import subprocess
import requests
import re
import ast
import tempfile
import shutil
import traceback
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import constants
import agent_state
import llm_interface
import comms
from core_registry import registry
import capabilities.base_tools  # Registers all tools

def build_dynamic_telemetry_message(state: Dict[str, Any], queue: List[Dict[str, Any]], task_desc: str, notices: List[str] = None, exclude_meta: bool = False) -> str:
    """Generates the minimalist HUD string wrapped in robust XML tags."""
    token_limit = constants.CONTEXT_WINDOW
    current_context = state.get("last_context_size", 0)
    context_pct = int((current_context / token_limit) * 100) if token_limit else 0

    current_turns = state.get("timeline_turns", 0)

    # Inject Memory Index and Pending Queue for turn-by-turn awareness
    memory_data = agent_state.safe_load_json(constants.MEMORY_STORE_PATH, {})
    keys = list(memory_data.get("entries", {}).keys())
    memory_index = ", ".join(keys) if keys else "None"
    
    pending_tasks = queue[1:6]
    queue_summary = "None"
    if pending_tasks:
        queue_summary = " | ".join([f"[{t.get('priority', 1)}] {t.get('description', '')[:30]}..." for t in pending_tasks])

    hud_content = f"[HUD | Context: {context_pct}% | Turns: {current_turns} | Queue: {len(queue)}]\n" \
                  f"CURRENT FOCUS: {task_desc}\n" \
                  f"UPCOMING TASKS: {queue_summary}\n" \
                  f"MEMORY INDEX: {memory_index}"

    # Piggyback system notices if any
    effective_notices = notices if notices is not None else agent_state.get_pending_system_notices()
    
    if exclude_meta and effective_notices:
        # Filter out all meta-instructions (System/Critical) to avoid context pollution
        effective_notices = [
            n for n in effective_notices 
            if not any(marker in n for marker in ["[SYSTEM", "[CRITICAL]"])
        ]

    interrupt_block = ""
    if effective_notices:
        msgs_str = "\n".join([f"{m}" for m in effective_notices])
        interrupt_block = f"\n\n<system_interrupt>\n{msgs_str.strip()}\n</system_interrupt>"

    return f"<ouroboros_hud>\n{hud_content}\n</ouroboros_hud>{interrupt_block}"


def build_static_system_prompt() -> str:
    identity = (constants.ROOT_DIR / "identity.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "identity.md").exists() else ""
    constitution = (constants.ROOT_DIR / "CONSTITUTION.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "CONSTITUTION.md").exists() else ""

    # P9: Static context for KV caching. Dynamic info is offloaded to the HUD piggyback.
    return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{constitution}

## CONVERSATIONAL CONTEXT
Full log available via `read_file` at {constants.CHAT_HISTORY_PATH}. If a new [CREATOR MESSAGE] lacks context, use `read_file` to review your conversational past before replying. ---

## PENDING QUEUE (Upcoming tasks)
See your `<ouroboros_hud>` for upcoming tasks. No pending tasks will be listed here to maximize KV cache efficiency. ---
"""


def process_scheduled_tasks(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not constants.SCHEDULED_TASKS_PATH.exists():
        return queue
    try:
        content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return queue

        scheduled = json.loads(content)
        now = time.time()
        due_tasks = [t for t in scheduled if now >= t.get("run_after", 0)]

        if due_tasks:
            pending_tasks = [t for t in scheduled if now < t.get("run_after", 0)]
            constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(pending_tasks, indent=2), encoding="utf-8")

            for t in due_tasks:
                t.pop("run_after", None)
                queue.append(t)

            queue.sort(key=lambda x: x.get("priority", 1), reverse=True)

            # FIX: Explicitly save the active queue to disk here so comms.py reads the fresh state
            constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            print(f"[Scheduler] Temporal shift: {len(due_tasks)} scheduled tasks moved to active queue.")
    except Exception as e:
        print(f"[Scheduler Error]: {e}")

    return queue

def _resolve_execution_context(
    state: Dict[str, Any],
    queue: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    if queue:
        top_task = queue[0]
        task_desc = f"CURRENT FOCUS: {top_task.get('description', 'Unknown')}"
    else:
        task_desc = "Queue is empty."

    active_tool_specs = registry.get_specs() # Grant access to all tools
    return task_desc, active_tool_specs

def _build_api_messages(
    task_desc: str,
    active_tool_specs: List[Dict[str, Any]],
    queue: List[Dict[str, Any]],
    state: Dict[str, Any],
    enrich: bool = True
) -> List[Dict[str, Any]]:
    system_prompt = build_static_system_prompt()
    api_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    raw_messages = agent_state.load_stream_messages()
    # P1 Integrity: Work on a copy to avoid corrupting the in-memory session cache
    # if the LLM call fails and needs to be retried.
    normalized = list(raw_messages)

    # P1 Integrity: Recursively pop ALL trailing assistant messages.
    # Backends like llamacpp/OpenAI reject payloads with consecutive assistant messages 
    # or assistant messages at the absolute end when expecting a new turn.
    while normalized and normalized[-1]["role"] == "assistant":
        normalized.pop()

    # Apply volatile enrichment ONLY to the in-memory payload (not log)
    # This is the transient Piggyback for Turn N awareness
    if enrich and normalized:
        telemetry = build_dynamic_telemetry_message(state, queue, task_desc)
        last_msg = normalized[-1].copy()
        last_msg["content"] = str(last_msg.get("content", "")) + f"\n\n{telemetry}"
        normalized[-1] = last_msg
    elif enrich and not normalized:
        telemetry = build_dynamic_telemetry_message(state, queue, task_desc)
        normalized.append({"role": "user", "content": telemetry})

    api_messages += normalized
    return api_messages


def _archive_old_turns() -> None:
    """
    P9: Batch Snapshot Shedding. 
    Shortens historical tool responses while preserving HUDs and interrupts.
    Optimization: We only move the 'Shed Cutoff' forward every 10 turns
    to ensure the KV cache remains hot for long stretches.
    """
    log_path = constants.MEMORY_DIR / "task_log_singular_stream.jsonl"
    if not log_path.exists():
        return

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]

        hd_window = constants.RETAIN_FULL_LAST_N
        current_cutoff = len(messages) - hd_window
        
        if current_cutoff < 10:
            return

        # Find the last message that was already 'solidified'
        last_shed_idx = -1
        for i in range(current_cutoff):
            if "[SYSTEM LOG: Historical" in str(messages[i].get("content", "")):
                last_shed_idx = i
        
        unshed_count = current_cutoff - (last_shed_idx + 1)
        if unshed_count < 10:
            # Not enough turns to justify a batch reload
            return

        modified = False
        # Solidify everything up to the cutoff
        for i in range(current_cutoff):
            msg = messages[i]
            if "[SYSTEM LOG: Historical" in str(msg.get("content", "")):
                continue

            # Apply shedding while preserving HUD/Interrupt tail
            role = msg.get("role")
            content_str = str(msg.get("content", ""))

            if role in ["tool", "assistant"]:
                # 1. Strip Reasoning
                if "thinking" in msg: msg.pop("thinking"); modified = True
                if "reasoning_content" in msg: msg.pop("reasoning_content"); modified = True

                # 2. Re-apply the shedding logic from llm_interface (Fixed Snapshot)
                hud_match = re.search(r"(<ouroboros_hud>.*?</ouroboros_hud>)", content_str, flags=re.DOTALL)
                interrupt_match = re.search(r"(<system_interrupt>.*?</system_interrupt>)", content_str, flags=re.DOTALL)
                
                hud_tail = (hud_match.group(1) if hud_match else "")
                interrupt_tail = (interrupt_match.group(1) if interrupt_match else "")

                if role == "tool" and len(content_str) > constants.TOOL_OUTPUT_TRIM_CHARS:
                    clean_head = re.sub(r"<ouroboros_hud>.*?</ouroboros_hud>", "", content_str, flags=re.DOTALL)
                    clean_head = re.sub(r"<system_interrupt>.*?</system_interrupt>", "", clean_head, flags=re.DOTALL).strip()
                    msg["content"] = f"[SYSTEM LOG: Historical output truncated ({len(clean_head)} chars).]\nPreview: {clean_head[:500]}...\n\n{hud_tail}\n\n{interrupt_tail}".strip()
                    modified = True
                
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        try:
                            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                            arg_mod = False
                            for key in ["content", "patch", "text", "code"]:
                                if key in args and isinstance(args[key], str) and len(args[key]) > constants.TOOL_ARG_TRIM_CHARS:
                                    args[key] = f"(... {len(args[key])} characters of {key} archived ...)"
                                    arg_mod = True
                            if arg_mod:
                                tc["function"]["arguments"] = json.dumps(args)
                                modified = True
                        except Exception: pass

        if modified:
            print(f"[Snapshot] Solidified {unshed_count} historical turns in prefix.")
            with open(log_path, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")
            if agent_state._session.get("current_task_id") == "singular_stream":
                agent_state._session["cached_messages"] = messages

    except Exception as e:
        print(f"[Snapshot Error] {e}")


def _route_tool_calls(
    message: Any,
    task_desc: str,
    state: Dict[str, Any],
    queue: List[Dict[str, Any]],
    persistent_hud: Optional[str] = None
) -> Tuple[bool, bool]:
    context_switch_triggered = False
    hibernating = False
    error_streak = state.get("error_streak", 0)
    tool_responses = []

    # Collect results
    tool_calls = message.tool_calls
    for i, tool_call in enumerate(tool_calls):
        name     = tool_call.function.name
        raw_args = tool_call.function.arguments
        safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"

        try:
            args   = json.loads(raw_args)
            result = registry.execute(name, args)
        except json.JSONDecodeError:
            result = "SYSTEM ERROR: Invalid JSON arguments."

        is_error = "Error:" in str(result) or "SYSTEM ERROR" in str(result)
        error_streak = error_streak + 1 if is_error else 0

        # Piggyback the HUD onto the LAST tool response IF persistence is triggered (Finding 22)
        content = str(result)
        if i == len(tool_calls) - 1 and persistent_hud:
            content = f"{content.strip()}\n\n{persistent_hud}"

        tool_responses.append({
            "role": "tool",
            "tool_call_id": safe_call_id,
            "name": name,
            "content": content
        })

    # ATOMIC FLUSH
    if agent_state.is_stream_empty():
        agent_state.append_stream_message({"role": "user", "content": "."})

    agent_state.append_stream_message(message.model_dump(exclude_unset=True))
    
    for response_msg in tool_responses:
        agent_state.append_stream_message(response_msg)

    # Signal processing
    if any("SYSTEM_SIGNAL_RESTART" in str(r["content"]) for r in tool_responses):
        sys.exit(0)
    if any("SYSTEM_SIGNAL_HIBERNATE" in str(r["content"]) for r in tool_responses):
        hibernating = True

    post_loop_state = agent_state.load_state()
    post_loop_state["error_streak"] = error_streak
    agent_state.save_state(post_loop_state)
    return context_switch_triggered, hibernating


def _determine_metacognition_params(state: Dict[str, Any], task_desc: str) -> Tuple[float, bool]:
    """Determines the optimal temperature and thinking mode based on error streaks and task content."""
    sys_temp_override = state.get("sys_temp")
    sys_think = True

    if sys_temp_override is None:
        error_streak = state.get("error_streak", 0)
        if error_streak >= 6:
            sys_temp, sys_think = 0.9, True
        elif error_streak >= 3:
            sys_temp, sys_think = 0.3, True
        elif any(keyword in task_desc.lower() for keyword in ["code", "script", "python", "bug", "refactor"]):
            sys_temp, sys_think = 0.6, True
        else:
            sys_temp = 0.8
    else:
        sys_temp = float(sys_temp_override)
    
    return sys_temp, sys_think


def _detect_cognitive_loop(messages: List[Dict[str, Any]], window: int = 6) -> Tuple[bool, Optional[str]]:
    """
    Analyzes the recent message stream for repetitive tool call patterns.
    Returns (is_looping, warning_message).
    """
    if len(messages) < window:
        return False, None

    recent_calls = []
    for msg in messages[-window:]:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", "")
                recent_calls.append((name, args))

    if not recent_calls:
        return False, None

    if len(recent_calls) >= 2 and recent_calls[-1] == recent_calls[-2]:
        return True, "Immediate Repetition: You just called the same tool with identical arguments."

    if len(recent_calls) >= 4:
        half = len(recent_calls) // 2
        if recent_calls[-half:] == recent_calls[-2*half:-half]:
            return True, f"Cyclic Pattern: You are repeating a sequence of {half} tool calls."

    return False, None

def main() -> None:
    agent_state.initialize_memory()
    print(f"Awaking Native ReAct Mode (JSONL). Model: {constants.MODEL} | Thinking: {'ON' if constants.ENABLE_THINKING else 'OFF'}")

    # WP1: Clean bootstrap
    state = agent_state.load_state()
    queue = agent_state.load_task_queue()

    while True:
        state = agent_state.load_state()
        queue = agent_state.load_task_queue()
        queue = process_scheduled_tasks(queue)
        state, queue = comms.poll_telegram(state, queue)

        if time.time() < state.get("wake_time", 0):
            if queue:
                state["wake_time"] = 0
                agent_state.save_state(state)
            else:
                try:
                    Path(constants.MEMORY_DIR / "task_log_singular_stream.jsonl").touch()
                except Exception: pass
                time.sleep(15)
                continue

        task_desc, active_tool_specs = \
            _resolve_execution_context(state, queue)

        agent_state.enforce_context_limits(state)
        state = agent_state.load_state() 

        state["timeline_turns"] = state.get("timeline_turns", 0) + 1
        agent_state.save_state(state)

        if state.get("force_fold"):
            active_tool_specs = [t for t in active_tool_specs if t["function"]["name"] == "fold_context"]
            state["force_fold"] = False 
            agent_state.save_state(state)


        sys_temp, sys_think = _determine_metacognition_params(state, task_desc)
        sys_top_p = state.get("sys_top_p", 0.95)

        try:
            # P9: Piggyback Persistence Logic
            token_limit = constants.CONTEXT_WINDOW
            current_context = state.get("last_context_size", 0)
            context_pct = int((current_context / token_limit) * 100) if token_limit else 0
            
            thresholds = [15, 30, 50, 60, 70, 75, 80, 85, 90, 95]
            last_threshold = state.get("last_hud_threshold", 0)
            crossed_threshold = next((t for t in thresholds if context_pct >= t and last_threshold < t), None)
            
            system_notices = agent_state.get_pending_system_notices()
            is_genesis = agent_state.is_stream_empty()
            queue_empty = not queue
            
            # Persistent HUD Piggyback (Only logged on events to preserve cache prefix on normal turns)
            persistent_hud = None
            if crossed_threshold is not None or system_notices or is_genesis or queue_empty:
                if crossed_threshold is not None: state["last_hud_threshold"] = crossed_threshold
                elif is_genesis: state["last_hud_threshold"] = 1
                agent_state.save_state(state)
                # Pass captured notices so they aren't lost even if cleared from disk early
                persistent_hud = build_dynamic_telemetry_message(state, queue, task_desc, notices=system_notices)

            # 1. Build messages for the LLM API call (Transiently enriched Turn N)
            api_messages = _build_api_messages(
                task_desc, active_tool_specs,
                queue, state, enrich=True
            )
            
            if system_notices:
                agent_state.clear_pending_system_notices()

            # LOOP DETECTION: Check if we are stuck in a tool-call cycle before calling LLM
            is_looping, loop_warn = _detect_cognitive_loop(api_messages)
            if is_looping:
                # Inject warning as a system interrupt to force reflection/folding
                agent_state.queue_system_notice(f"[CRITICAL]: Cognitive Loop Detected! {loop_warn}")
                # Force a fold to clear the cycle and reset reasoning
                state["force_fold"] = True
                agent_state.save_state(state)
                print(f"[Loop Detector] {loop_warn}. Forcing context fold.")
                continue


            # 2. Call LLM
            response = llm_interface.call_llm(api_messages, active_tool_specs, None, sys_temp, sys_top_p, 1.0, sys_think)
            message  = response.choices[0].message

            # WP: Update metrics
            agent_state.update_global_metrics(state, queue, response)

            # P9 Maintenance: Archive old turns periodically to solidify prefix
            _archive_old_turns()

            # Emergency save logic
            is_emergency_save = bool(message.tool_calls and any(tc.function.name == "fold_context" for tc in message.tool_calls))

            if is_emergency_save:
                # P9 Optimization: If we are folding, rebuild the persistent HUD but exclude the meta-reflex notices.
                # This ensures we don't pollute the new clean context with the very warning that triggered the fold,
                # while still preserving any genuine external interrupts (like Telegram messages).
                persistent_hud = build_dynamic_telemetry_message(state, queue, task_desc, notices=system_notices, exclude_meta=True)

            if not is_emergency_save:
                agent_state.enforce_context_limits(state)
                state = agent_state.load_state()
                if state.get("force_fold"): continue

            if message.tool_calls:
                context_switch, hibernating = _route_tool_calls(message, task_desc, state, queue, persistent_hud=persistent_hud)
                if context_switch or hibernating: continue
            else:
                content = message.model_dump(exclude_unset=True)
                if persistent_hud:
                    content["content"] = str(content.get("content") or "") + f"\n\n{persistent_hud}"
                agent_state.append_stream_message(content)
                time.sleep(0.5)

            time.sleep(2)

        except Exception as e:
            try:
                constants.CRASH_LOG_PATH.write_text(str(e), encoding="utf-8")
            except Exception: pass

            # P5: Fail Fast on structural/fatal errors.
            fatal_types = (AttributeError, ImportError, NameError, SyntaxError, TypeError)

            # Is it a structural Python error OR a fatal HTTP exception (not just a result string)?
            is_http_fatal = ("400" in str(e) or "500" in str(e)) and not any(kw in str(e).lower() for kw in ["telegram", "searxng", "bash"])

            if isinstance(e, fatal_types) or is_http_fatal:
                print(f"\033[91m[FATAL]: {type(e).__name__}: {e}. Exiting for watchdog recovery.\033[0m")
                # WP: Print traceback for fatal errors to help Creator debug
                traceback.print_exc()
                sys.exit(1)

            print(f"[ERROR]: {e}. Recovering in 2s...")
            time.sleep(2)

if __name__ == "__main__":
    main()
