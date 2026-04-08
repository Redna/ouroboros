import os
import json
import re
import time
from typing import List, Dict, Any

from openai import OpenAI
import constants
import agent_state

client = OpenAI(base_url=constants.API_BASE, api_key="sk-not-required", timeout=600.0)

def call_llm(messages, tools=None, requested_model=None, temperature=0.8, top_p=0.95, presence_penalty=1.0, think=True):
    active_model = requested_model if requested_model else constants.DEFAULT_MODEL
    
    # WP: Message Normalization (Finding: llamacpp 400s on consecutive system messages)
    normalized_messages = []
    for msg in messages:
        if normalized_messages and normalized_messages[-1]["role"] == "system" and msg["role"] == "system":
            normalized_messages[-1]["content"] += f"\n\n{msg['content']}"
        else:
            normalized_messages.append(msg)
    
    # Force the agent to use its agency if tools are available
    tool_choice = "required" if tools else None
    
    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=normalized_messages,
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
    """
    P9: Absolute Prefix Stability. 
    To maintain 100% KV cache hits, we NEVER modify historical messages.
    Shedding is now handled either proactively at logging time or 
    globally during a fold_context event.
    """
    processed = []

    for i, msg in enumerate(messages):
        new_msg = msg.copy()
        role = new_msg.get("role")

        # WP: Prefix-safe prefill fix.
        # We ONLY remove reasoning from the absolute last message IF it is an assistant role,
        # as llamacpp cannot handle a prompt ending in reasoning_content when expecting a continuation.
        # This is a transient fix for the current turn only.
        if i == len(messages) - 1 and role == "assistant":
            if "thinking" in new_msg:
                new_msg.pop("thinking")
            if "reasoning_content" in new_msg:
                new_msg.pop("reasoning_content")

        processed.append(new_msg)

    return processed

