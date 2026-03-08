"""
Ouroboros — LLM client.

The only module that communicates with the LLM API.
Targets any OpenAI-compatible endpoint (vLLM, OpenAI, etc.)
configured via VLLM_BASE_URL / VLLM_API_KEY / OUROBOROS_MODEL.

Contract: chat(), vision_query(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

# --- Observability Setup ---
# Set up a dedicated file logger for the LLM client
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG) # Set to DEBUG to capture detailed payloads

# Create a file handler
log_file = os.environ.get("OUROBOROS_LOG_FILE", "ouroboros_llm.log")
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)

# Create a formatter and add it to the handler
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
log.addHandler(file_handler)
# ---------------------------

# Default light model — override with OUROBOROS_MODEL_LIGHT env var.
# Set to a small/fast model available on your vLLM server.
DEFAULT_LIGHT_MODEL = os.environ.get("OUROBOROS_MODEL_LIGHT", "")

# Default max tokens for completions — override with OUROBOROS_MAX_TOKENS.
DEFAULT_MAX_TOKENS = int(os.environ.get("OUROBOROS_MAX_TOKENS", "16384"))


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate token usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    # Cost is always 0 for local vLLM — kept for structural compatibility
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


class LLMClient:
    """OpenAI-compatible LLM client targeting a vLLM endpoint.

    Configuration (via environment variables):
        VLLM_BASE_URL      Base URL of the OpenAI-compatible API
                           e.g. http://localhost:8000/v1
        VLLM_API_KEY       API key (default: "token" — vLLM default)
        OUROBOROS_MODEL    Model name as registered in vLLM
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self._api_key = (
            api_key
            or os.environ.get("VLLM_API_KEY", "")
            or "token"  # vLLM default when no auth is configured
        )
        self._base_url = (
            base_url
            or os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            from ouroboros.tracing import openai
            self._client = openai.OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
            )
        return self._client

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: Optional[int] = None,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns (response_message_dict, usage_dict).

        usage_dict contains prompt_tokens, completion_tokens, total_tokens.
        cost is always 0 for local vLLM.
        """
        client = self._get_client()

        # Use explicitly provided max_tokens or fallback to global default
        _max_tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": _max_tokens,
        }
        _disable_tools = os.environ.get("OUROBOROS_DISABLE_TOOLS", "0").strip() == "1"
        if tools and not _disable_tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        # Disable thinking mode by default (Qwen3 / models that support it).
        # <think> blocks bloat context and interfere with tool-call parsing.
        # Set OUROBOROS_ENABLE_THINKING=1 to re-enable.
        _enable_thinking = os.environ.get("OUROBOROS_ENABLE_THINKING", "0").strip() == "1"
        if not _enable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        # --- Logging the Request ---
        log.info(f"LLM Request | Model: {model} | Tools provided: {bool(tools and not _disable_tools)}")
        log.debug(f"Request Messages:\n{json.dumps(messages, indent=2)}")
        if tools and not _disable_tools:
            log.debug(f"Request Tools:\n{json.dumps(tools, indent=2)}")
        # ---------------------------

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            log.error(f"LLM Call Failed: {str(e)}")
            raise

        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Normalise cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Cost is N/A for local vLLM — always 0
        usage.setdefault("cost", 0.0)

        # --- Logging the Response ---
        log.info(f"LLM Response | Usage: {usage}")
        log.debug(f"Response Message:\n{json.dumps(msg, indent=2)}")
        # ----------------------------

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "",
        max_tokens: Optional[int] = None,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """Send a vision query to an LLM. Lightweight — no tools, no loop."""
        if not model:
            model = self.default_model()

        # Use explicitly provided max_tokens or fallback to global default (lower for vision usually, but user wants more)
        _max_tokens = max_tokens if max_tokens is not None else min(4096, DEFAULT_MAX_TOKENS)

        # --- Logging Vision Request ---
        log.info(f"Vision Request | Model: {model} | Images: {len(images)}")
        log.debug(f"Vision Prompt: {prompt}")
        # ------------------------------

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=_max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the default model name from env."""
        return os.environ.get("OUROBOROS_MODEL", "")

    def available_models(self) -> List[str]:
        """Return list of configured model names (for switch_model tool)."""
        main = os.environ.get("OUROBOROS_MODEL", "")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [m for m in [main, code, light] if m]
        # Deduplicate while preserving order
        seen: set = set()
        result = []
        for m in models:
            if m not in seen:
                seen.add(m)
                result.append(m)
        return result