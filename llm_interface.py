import os
import json
import re
import time
from typing import List, Dict, Any

from openai import OpenAI
import constants
import agent_state

client = OpenAI(base_url=constants.API_BASE, api_key="sk-not-required", timeout=1800.0)

def call_llm(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] = None, tool_choice: Any = None, temperature: float = 0.7, top_p: float = 0.95, frequency_penalty: float = 0.0, think: bool = False):
    """
    Standard Ouroboros LLM interface.
    """
    model = constants.MODEL
    
    # Apply prefix-stable shedding
    payload_messages = shed_heavy_payloads(messages)
    
    body = {
        "model": model,
        "messages": payload_messages,
        "temperature": temperature,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "stream": False
    }
    
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice

    # Backend-specific thinking/reasoning parameters
    if think:
        body["extra_body"] = {"thinking": True}

    response = client.chat.completions.create(**body)
    return response

def redact_secrets(text: str) -> str:
    if not text: return text
    if constants.TELEGRAM_BOT_TOKEN: text = text.replace(constants.TELEGRAM_BOT_TOKEN, "[REDACTED]")
    if constants.GITHUB_TOKEN: text = text.replace(constants.GITHUB_TOKEN, "[REDACTED]")
    return re.sub(r"\d{8,10}:[a-zA-Z0-9_-]{35}", "[REDACTED_TOKEN]", text)

def shed_heavy_payloads(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    P9: Prefix-safe shedding logic.
    We shorten the tool response but leave the HUD and interruptions exactly as they are.
    Historical messages are processed only once they pass the HD window (N-3).
    """
    processed = []
    
    for i, msg in enumerate(messages):
        new_msg = msg.copy()
        role = new_msg.get("role")
        content_str = str(new_msg.get("content", ""))

        # WP: Prefix-safe prefill fix for the current turn
        if i == len(messages) - 1 and role == "assistant":
            if "thinking" in new_msg: new_msg.pop("thinking")
            if "reasoning_content" in new_msg: new_msg.pop("reasoning_content")
            processed.append(new_msg)
            continue

        # Don't touch the High-Definition window (last 3 turns)
        if i >= len(messages) - 3:
            processed.append(new_msg)
            continue

        # Shed old messages: Truncate Tool/Assistant but preserve the HUD tail
        if role in ["tool", "assistant"]:
            # Extract HUD/Interruption blocks if present
            hud_match = re.search(r"(<ouroboros_hud>.*?</ouroboros_hud>)", content_str, flags=re.DOTALL)
            interrupt_match = re.search(r"(<system_interrupt>.*?</system_interrupt>)", content_str, flags=re.DOTALL)
            
            hud_tail = (hud_match.group(1) if hud_match else "")
            interrupt_tail = (interrupt_match.group(1) if interrupt_match else "")
            
            # 1. Archive Reasoning/Thinking
            if "thinking" in new_msg: new_msg.pop("thinking")
            if "reasoning_content" in new_msg: new_msg.pop("reasoning_content")

            # 2. Truncate heavy tool arguments in assistant calls
            if role == "assistant" and new_msg.get("tool_calls"):
                for tc in new_msg["tool_calls"]:
                    try:
                        args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                        for key in ["content", "patch", "text", "code"]:
                            if key in args and isinstance(args[key], str) and len(args[key]) > constants.TOOL_ARG_TRIM_CHARS:
                                args[key] = f"(... {len(args[key])} characters of {key} archived ...)"
                        tc["function"]["arguments"] = json.dumps(args)
                    except Exception: pass

            # 3. Truncate Tool Output while keeping the HUD
            if role == "tool" and len(content_str) > constants.TOOL_OUTPUT_TRIM_CHARS:
                # Truncate only the "Head" (the actual tool result)
                clean_head = re.sub(r"<ouroboros_hud>.*?</ouroboros_hud>", "", content_str, flags=re.DOTALL)
                clean_head = re.sub(r"<system_interrupt>.*?</system_interrupt>", "", clean_head, flags=re.DOTALL).strip()
                
                new_msg["content"] = f"[SYSTEM LOG: Historical output truncated ({len(clean_head)} chars).]\nPreview: {clean_head[:500]}...\n\n{hud_tail}\n\n{interrupt_tail}".strip()

        processed.append(new_msg)
        
    return processed
