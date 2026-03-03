"""
Ouroboros — LLM client.

The only module that communicates with the LLM API.
Targets any OpenAI-compatible endpoint (vLLM, OpenAI, etc.)
configured via VLLM_BASE_URL / VLLM_API_KEY / OUROBOROS_MODEL.

Contract: chat(), vision_query(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Default light model — override with OUROBOROS_MODEL_LIGHT env var.
# Set to a small/fast model available on your vLLM server.
DEFAULT_LIGHT_MODEL = os.environ.get("OUROBOROS_MODEL_LIGHT", "")


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
            from openai import OpenAI
            self._client = OpenAI(
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
        max_tokens: int = 8192,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns (response_message_dict, usage_dict).

        usage_dict contains prompt_tokens, completion_tokens, total_tokens.
        cost is always 0 for local vLLM.
        """
        client = self._get_client()

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
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

        resp = client.chat.completions.create(**kwargs)
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

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."}
                - {"base64": "<b64>", "mime": "image/png"}
            model: VLM-capable model ID (defaults to OUROBOROS_MODEL)
            max_tokens: Max response tokens
            reasoning_effort: Effort level (unused for vLLM, kept for API compat)

        Returns:
            (text_response, usage_dict)
        """
        if not model:
            model = self.default_model()

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
            max_tokens=max_tokens,
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
