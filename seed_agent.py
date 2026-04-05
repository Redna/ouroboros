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
from typing import List, Dict, Any, Optional, Tuple

import constants
import agent_state
import llm_interface
import comms
from core_registry import registry
import capabilities.base_tools  # Registers all tools

def build_dynamic_telemetry_message(state: Dict[str, Any], queue: List[Dict[str, Any]], task_desc: str) -> str:
    """Generates the minimalist HUD string wrapped in robust XML tags."""
    token_limit = constants.CONTEXT_WINDOW
    current_context = state.get("last_context_size", 0)
    context_pct = int((current_context / token_limit) * 100) if token_limit else 0

    current_turns = state.get("timeline_turns", 0)

    hud_content = f"[HUD | Context: {context_pct}% | Turns: {current_turns} | Queue: {len(queue)}] | {task_desc}"

    # Piggyback creator messages and system notices if any (Finding 18, 20)
    pending_msgs = agent_state.get_pending_creator_messages()
    system_notices = agent_state.get_pending_system_notices()

    interrupt_block = ""
    if pending_msgs or system_notices:
        msgs_str = ""
        if pending_msgs:
            msgs_str += "\n[CREATOR MESSAGES]\n" + "\n".join([f"- {m}" for m in pending_msgs])
        if system_notices:
            msgs_str += "\n[SYSTEM NOTICES]\n" + "\n".join([f"- {m}" for m in system_notices])

        interrupt_block = f"\n\n<system_interrupt>\n{msgs_str.strip()}\nAddress these immediately in your next response.\n</system_interrupt>"

    return f"<ouroboros_hud>\n{hud_content}\n</ouroboros_hud>{interrupt_block}"


def build_static_system_prompt(active_tool_specs: List[Dict[str, Any]], queue: List[Dict[str, Any]]) -> str:
    identity = (constants.ROOT_DIR / "identity.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "identity.md").exists() else ""
    constitution = (constants.ROOT_DIR / "CONSTITUTION.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "CONSTITUTION.md").exists() else ""

    # Inject Memory Index (Finding 19)
    memory_data = agent_state.safe_load_json(constants.MEMORY_STORE_PATH, {})
    keys = list(memory_data.get("entries", {}).keys())
    memory_index = "\n".join([f"- {k}" for k in keys]) if keys else "No memories stored."

    # NEW: Progressive Disclosure Hook for Conversation
    chat_history = agent_state.load_chat_history()
    if chat_history:
        last_msg_time = chat_history[-1].get("timestamp", "Unknown")
        chat_metadata = f"Last creator interaction: {last_msg_time}. Full log available via `read_file` at {constants.CHAT_HISTORY_PATH}."
    else:
        chat_metadata = "No prior creator interaction."

    # Inject Pending Queue (Skip the first item since it's the CURRENT FOCUS in the HUD)
    pending_tasks = queue[1:6] # Show up to 5 upcoming tasks to save tokens
    if pending_tasks:
        queue_str = "\n".join([f"- [Pri: {t.get('priority', 1)}] {t.get('description', '')}" for t in pending_tasks])
        if len(queue) > 6:
            queue_str += f"\n... and {len(queue) - 6} more hidden tasks."
    else:
        queue_str = "No pending tasks."

    return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{constitution}

## CONVERSATIONAL CONTEXT
{chat_metadata}
If a new [CREATOR MESSAGE] lacks context, use `read_file` to review your conversational past before replying.

## MEMORY INDEX (Available for recall_memory)
{memory_index}

## PENDING QUEUE (Upcoming tasks)
{queue_str}
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
        # P5 Minimalism & P0 Agency: Purely factual telemetry.
        # The agent must rely on its Constitution to decide what to do next.
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
    system_prompt = build_static_system_prompt(active_tool_specs, queue)
    api_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    raw_messages = agent_state.load_stream_messages()
    normalized = raw_messages

    if normalized and normalized[-1]["role"] == "assistant":
        # WP: Structural Integrity (Crash recovery)
        # If the log ends in an assistant message, we either crashed mid-tools or the
        # previous session didn't clean up correctly. Popping it allows the agent to
        # re-evaluate and re-issue the plan cleanly on this run.
        normalized.pop()

    shedded = llm_interface.shed_heavy_payloads(normalized)

    # Volatile HUD Injection: Append telemetry dynamically to the LAST available message
    if enrich and shedded:
        telemetry = build_dynamic_telemetry_message(state, queue, task_desc)
        last_msg = shedded[-1]

        # Ensure we don't break JSON parsing if the last message is a tool call
        if last_msg.get("role") in ["user", "tool"]:
            last_msg["content"] = str(last_msg.get("content", "")) + f"\n\n{telemetry}"

    api_messages += shedded

    return api_messages


def _route_tool_calls(
    message: Any,
    task_desc: str,
    state: Dict[str, Any],
    queue: List[Dict[str, Any]]
) -> Tuple[bool, bool]:
    context_switch_triggered = False
    hibernating = False
    error_streak = state.get("error_streak", 0)
    tool_responses = []

    # Collect results to find the last one for telemetry piggybacking
    tool_calls = message.tool_calls
    for i, tool_call in enumerate(tool_calls):
        name     = tool_call.function.name
        raw_args = tool_call.function.arguments

        safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"

        try:
            args   = json.loads(raw_args)
            result = registry.execute(name, args, call_id=safe_call_id)
        except json.JSONDecodeError:
            result = "SYSTEM ERROR: Invalid JSON arguments."

        is_error = "Error:" in str(result) or "SYSTEM ERROR" in str(result)
        error_streak = error_streak + 1 if is_error else 0

        tool_responses.append({
            "role": "tool",
            "tool_call_id": safe_call_id,
            "name": name,
            "content": str(result)
        })

    # ATOMIC FLUSH: Write to the task log BEFORE any exit signals are processed
    # P1 Continuity: Ensure the log ALWAYS starts with a user message (Genesis Fix)
    if agent_state.is_stream_empty():
        agent_state.append_stream_message({"role": "user", "content": "Begin Genesis execution."})

    agent_state.append_stream_message(message.model_dump(exclude_unset=True))
    for response_msg in tool_responses:
        agent_state.append_stream_message(response_msg)

    # Now process signals that would terminate the loop
    if any("SYSTEM_SIGNAL_RESTART" in str(r["content"]) for r in tool_responses):
        sys.exit(0)

    if any("SYSTEM_SIGNAL_HIBERNATE" in str(r["content"]) for r in tool_responses):
        hibernating = True

    post_loop_state = agent_state.load_state()
    post_loop_state["error_streak"] = error_streak
    agent_state.save_state(post_loop_state)
    return context_switch_triggered, hibernating


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
                # Heartbeat for watchdog (Finding 12: Prevent false stall detections)
                try:
                    Path(constants.MEMORY_DIR / "task_log_singular_stream.jsonl").touch()
                except Exception: pass
                time.sleep(15)
                continue

        task_desc, active_tool_specs = \
            _resolve_execution_context(state, queue)

        # Tier 1 Context Safety (Pre-emptive)
        # If we already know the context is full from the last turn (or window was reduced),
        # trigger the reflex BEFORE calling the LLM to avoid 400 errors.
        agent_state.enforce_context_limits(state)
        state = agent_state.load_state() # Reload to catch force_fold

        # WP: Explicit Turn Increment (Finding 17: prevent infinite fold loops)
        state["timeline_turns"] = state.get("timeline_turns", 0) + 1
        agent_state.save_state(state)

        # FORCE FOLD MODE: Restrict LLM to fold_context only.
        # Set by autonomic_fold() when context is critically full.
        # tool_choice='required' (set in call_llm) means the LLM MUST call it.
        if state.get("force_fold"):
            print(f"\033[91m[System] Force Fold Mode: tool surface restricted to fold_context only.\033[0m")
            active_tool_specs = [t for t in active_tool_specs if t["function"]["name"] == "fold_context"]
            state["force_fold"] = False  # Unset — applies for this one turn only
            agent_state.save_state(state)


        sys_temp_override = state.get("sys_temp")
        sys_top_p = state.get("sys_top_p", 0.95)
        sys_think = state.get("sys_think", True)

        if sys_temp_override is None:
            error_streak = state.get("error_streak", 0)
            if error_streak >= 6:
                print(f"\033[93m[Metacognition] Critical error streak ({error_streak}). Triggering Creative Escape (Temp 0.9).\033[0m")
                sys_temp, sys_think = 0.9, True
                # Jolt the agent exactly once when it hits the threshold
                if error_streak == 6:
                    agent_state.queue_system_notice(
                        "[SYSTEM OVERRIDE]: You are in a Cognitive Death Spiral. Your previous approaches have repeatedly failed. "
                        "Do NOT try the exact same code patch again. Step back, read the file again, search for documentation, or drastically pivot your strategy."
                    )
            elif error_streak >= 3:
                print(f"[Metacognition] High error streak ({error_streak}). Auto-tuning temperature to 0.3 for precision.")
                sys_temp, sys_think = 0.3, True
            elif any(keyword in task_desc.lower() for keyword in ["code", "script", "python", "bug", "refactor"]):
                sys_temp, sys_think = 0.6, True
            else:
                sys_temp = 0.8
        else:
            sys_temp = float(sys_temp_override)

        try:
            current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")
            # 1. Build messages for the LLM API call (Full HUD)
            api_messages = _build_api_messages(
                task_desc, active_tool_specs,
                queue, state, enrich=True
            )

            # 2. Call LLM
            response = llm_interface.call_llm(api_messages, active_tool_specs, None, sys_temp, sys_top_p, 1.0, sys_think)
            message  = response.choices[0].message

            # WP: Update metrics BEFORE enforcing limits so thresholds use current data (Finding 11)
            agent_state.update_global_metrics(state, queue, response)

            # Let fold_context execute even when at BREACH threshold.
            is_emergency_save = bool(
                message.tool_calls and
                any(tc.function.name == "fold_context" for tc in message.tool_calls)
            )

            # Let the state module enforce limits internally
            if not is_emergency_save:
                agent_state.enforce_context_limits(state)
                # Reload state to catch any force_fold flags just set by enforce_context_limits
                state = agent_state.load_state()

                if state.get("force_fold"):
                    # The system just triggered a reflex. Skip executing the LLM's current tools.
                    continue

            if message.tool_calls:
                # Atomic Logging: _route_tool_calls will handle logging both assistant + tool responses
                context_switch, hibernating = _route_tool_calls(message, task_desc, state, queue)

                # V5: Clear piggybacked metadata after they've been injected into HUD and seen
                agent_state.clear_pending_creator_messages()
                agent_state.clear_pending_system_notices()

                if context_switch or hibernating:
                    continue
            else:
                # No tools - log current assistant turn immediately
                agent_state.append_stream_message(message.model_dump(exclude_unset=True))

                # V5: Clear piggybacked metadata after they've been injected into HUD and seen
                agent_state.clear_pending_creator_messages()
                agent_state.clear_pending_system_notices()

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

            if isinstance(e, fatal_types) or is_http_fatal or "template" in str(e).lower():
                print(f"\033[91m[FATAL]: {type(e).__name__}: {e}. Exiting for watchdog recovery.\033[0m")
                sys.exit(1)

            print(f"[ERROR]: {e}. Recovering in 2s...")
            time.sleep(2)

if __name__ == "__main__":
    main()