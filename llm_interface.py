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
    
    # Force the agent to use its agency if tools are available
    tool_choice = "required" if tools else None
    
    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            extra_body={"cache_prompt": True, "top_k": 20}
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
    
    # Agency-First: Thinking/Reasoning shedding for older turns (n-3)
    thinking_cutoff_idx = len(messages) - 3
    
    for i, msg in enumerate(messages):
        new_msg = msg.copy()
        role = new_msg.get("role")
        
        # Strip Thinking from older assistant turns (Finding 10)
        # OR if it's the absolute last message (Assistant Prefill) to avoid 400 errors (Finding: Prefill incompatibility)
        if role == "assistant":
            if i < thinking_cutoff_idx or i == len(messages) - 1:
                if "thinking" in new_msg:
                    new_msg.pop("thinking")
                if "reasoning_content" in new_msg:
                    new_msg.pop("reasoning_content")


        if i == 0 or i >= cutoff_idx:
            processed.append(new_msg)
            continue
            
        content_str = str(new_msg.get("content", ""))
        
        # WP: Telemetry Compression (Context Rot Prevention)
        # Robust XML parsing for historical telemetry (Finding 14)
        if "<ouroboros_hud>" in content_str:
            def replace_hud(match):
                telemetry_block = match.group(1)
                lines = telemetry_block.splitlines()
                # Extract Physiology Heartbeat
                heartbeat = next((l for l in lines if "[HUD" in l), "[HEARTBEAT: Metrics Archived]")
                return f"[SYSTEM LOG: Historical Telemetry Archived: {heartbeat}]"
                
            new_msg["content"] = re.sub(r"<ouroboros_hud>(.*?)</ouroboros_hud>", replace_hud, content_str, flags=re.DOTALL)
            content_str = new_msg["content"]

        if role == "tool" and len(content_str) > constants.TOOL_OUTPUT_TRIM_CHARS:
            new_msg["content"] = f"[SYSTEM LOG: Historical output truncated ({len(content_str)} chars).]\nPreview: {content_str[:500]}..."
            
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
