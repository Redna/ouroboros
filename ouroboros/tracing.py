"""
Ouroboros — Langfuse tracing integration (opt-in).

Provides graceful fallback: when LANGFUSE_HOST is not set or the langfuse
package is not installed, all exports become no-ops and the agent runs
exactly as before.

Exports:
    observe         — decorator (real or no-op)
    langfuse_context — context manager for trace metadata (or None)
    openai          — openai module (instrumented or stock)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detect whether Langfuse tracing should be active
# ---------------------------------------------------------------------------
_LANGFUSE_ENABLED = bool(os.environ.get("LANGFUSE_HOST"))

if _LANGFUSE_ENABLED:
    try:
        from langfuse.decorators import observe as _observe  # noqa: F401
        from langfuse.decorators import langfuse_context as _langfuse_context  # noqa: F401
        from langfuse import openai as _openai_mod  # instrumented drop-in

        observe = _observe
        langfuse_context = _langfuse_context
        openai = _openai_mod
        log.info("Langfuse tracing enabled → %s", os.environ["LANGFUSE_HOST"])
    except ImportError:
        log.warning(
            "LANGFUSE_HOST is set but the 'langfuse' package is not installed. "
            "Tracing is disabled. Install with: uv pip install langfuse"
        )
        _LANGFUSE_ENABLED = False

if not _LANGFUSE_ENABLED:
    # ------------------------------------------------------------------
    # No-op fallbacks — zero overhead when Langfuse is not configured
    # ------------------------------------------------------------------
    import openai as _stock_openai  # type: ignore[no-redef]

    openai = _stock_openai  # type: ignore[assignment]
    langfuse_context = None  # type: ignore[assignment]

    def observe(*args, **kwargs):  # type: ignore[misc]
        """No-op decorator when Langfuse is not available."""
        def _decorator(fn):
            return fn
        # Support both @observe and @observe() syntax
        if args and callable(args[0]):
            return args[0]
        return _decorator


def is_enabled() -> bool:
    """Return whether Langfuse tracing is currently active."""
    return _LANGFUSE_ENABLED
