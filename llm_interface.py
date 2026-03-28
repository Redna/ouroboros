import os
import json
import re
import time
from typing import List, Dict, Any, Optional

from openai import OpenAI
import constants
import agent_state

client = OpenAI(base_url=constants.API_BASE, api_key="sk-not-required", timeout=600.0)

def call_llm(messages, tools=None, requested_model=None, temperature=0.8, top_p=0.95, presence_penalty=1.0, think=True):
    active_model = requested_model if requested_model else constants.DEFAULT_MODEL
    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": think}}
        )
        agent_state._session["is_first_call"] = False
        return response
    except Exception as e:
        if agent_state._session.get("is_first_call", True):
            print(f"FATAL: constants.DEFAULT_MODEL is unreachable. Shutting down. Error: {e}")
            import sys
            sys.exit(1)
        raise e

def redact_secrets(text: str) -> str:
    if not text: return text
    if constants.TELEGRAM_BOT_TOKEN: text = text.replace(constants.TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if constants.GITHUB_TOKEN: text = text.replace(constants.GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

def shed_heavy_payloads(messages: List[Dict[str, Any]], retain_full_last_n: int = constants.RETAIN_FULL_LAST_N) -> List[Dict[str, Any]]:
    processed = []
    cutoff_idx = len(messages) - retain_full_last_n
    
    for i, msg in enumerate(messages):
        if i == 0 or i >= cutoff_idx:
            processed.append(msg)
            continue
            
        new_msg = msg.copy()
        role = new_msg.get("role")
        content_str = str(new_msg.get("content", ""))
        
        if role == "tool" and len(content_str) > constants.TOOL_OUTPUT_TRIM_CHARS:
            new_msg["content"] = f"[SYSTEM LOG: Historical output truncated ({len(content_str)} chars).]\nPreview: {content_str[:500]}..."
            
        elif role == "user" and "## CURRENT TELEMETRY" in content_str:
            if len(content_str) > constants.SYSTEM_METRICS_TRIM_CHARS:
                header = "## CURRENT TELEMETRY"
                # Preserve the [PHYSIOLOGY] line as a heartbeat for rationale
                lines = content_str.splitlines()
                heartbeat = next((l for l in lines if "[PHYSIOLOGY]" in l), "[HEARTBEAT: Metrics Archived]")
                prefix = content_str.split(header)[0].strip()
                new_msg["content"] = f"{prefix}\n{header}: {heartbeat} (Old structure archived to save tokens)"
            
        elif role == "assistant" and new_msg.get("tool_calls"):
            trimmed_calls = []
            for tc in new_msg["tool_calls"]:
                new_tc = tc.copy()
                try:
                    args = json.loads(new_tc.get("function", {}).get("arguments", "{}"))
                    for key in ["content", "patch", "text", "code"]:
                        if key in args and isinstance(args[key], str) and len(args[key]) > constants.TOOL_ARG_TRIM_CHARS:
                            args[key] = f"(... {len(args[key])} characters of {key} archived ...)"
                    new_tc["function"]["arguments"] = json.dumps(args)
                except Exception:
                    pass
                trimmed_calls.append(new_tc)
            new_msg["tool_calls"] = trimmed_calls
            
        processed.append(new_msg)
        
    return processed

def _normalize_message_history(messages: List[Dict[str, Any]], task_id: str) -> List[Dict[str, Any]]:
    """Enforce user-start, merge adjacent same-role messages, and heal dangling states."""
    # Enforce user start
    while messages and messages[0].get("role") != "user":
        messages.pop(0)
    if not messages:
        return []

    # Merge adjacent non-tool messages
    normalized: List[Dict[str, Any]] = []
    for msg in messages:
        if not normalized:
            normalized.append(msg)
            continue
        last = normalized[-1]
        role, last_role = msg.get("role"), last.get("role")
        if role == last_role and role in ["user", "assistant"] and not msg.get("tool_calls") and not last.get("tool_calls"):
            last["content"] = f"{last.get('content', '')}\n{msg.get('content', '')}".strip()
            continue
        normalized.append(msg)

    # Note: Dangling state healing (append_task_message) is tricky here 
    # as it might cause circular imports if agent_state.py is involved.
    # We will assume seed_agent handles the appending for now.
    return normalized
