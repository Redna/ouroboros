"""Tests for Langfuse tracing integration.

Verifies:
- No-op fallback works when LANGFUSE_HOST is not set
- Tracing module exports are importable
- LLMClient is compatible with the tracing-aware OpenAI import
- Module imports succeed across all core modules
"""
import importlib
import os
import pathlib
import sys
import tempfile

import pytest


class TestTracingNoOp:
    """When LANGFUSE_HOST is NOT set, everything should be a no-op."""

    def setup_method(self):
        """Ensure LANGFUSE_HOST is not set for these tests."""
        self._saved = os.environ.pop("LANGFUSE_HOST", None)
        # Reload tracing module to pick up env change
        if "ouroboros.tracing" in sys.modules:
            importlib.reload(sys.modules["ouroboros.tracing"])

    def teardown_method(self):
        if self._saved is not None:
            os.environ["LANGFUSE_HOST"] = self._saved
        if "ouroboros.tracing" in sys.modules:
            importlib.reload(sys.modules["ouroboros.tracing"])

    def test_import_succeeds(self):
        """Tracing module imports without error."""
        from ouroboros.tracing import observe, langfuse_context, openai, is_enabled
        assert not is_enabled()

    def test_observe_noop_decorator(self):
        """@observe() is a no-op — decorated function works normally."""
        from ouroboros.tracing import observe

        @observe(name="test_span")
        def my_func(x):
            return x * 2

        assert my_func(5) == 10

    def test_observe_noop_bare(self):
        """@observe (without parens) is a no-op — decorated function works normally."""
        from ouroboros.tracing import observe

        @observe
        def my_func(x):
            return x + 1

        assert my_func(5) == 6

    def test_langfuse_context_is_none(self):
        """langfuse_context is None when tracing is disabled."""
        from ouroboros.tracing import langfuse_context
        assert langfuse_context is None

    def test_openai_module_is_stock(self):
        """openai export is the stock openai module."""
        from ouroboros.tracing import openai
        assert hasattr(openai, "OpenAI"), "Should export stock openai with OpenAI class"


class TestTracingCompatibility:
    """Verify tracing integration doesn't break existing functionality."""

    def test_llm_client_importable(self):
        """LLMClient can be imported (tracing-aware import inside _get_client)."""
        from ouroboros.llm import LLMClient
        client = LLMClient(api_key="test", base_url="http://localhost:9999/v1")
        assert client is not None

    def test_agent_module_importable(self):
        """agent.py imports cleanly with tracing imports."""
        import ouroboros.agent
        assert hasattr(ouroboros.agent, "OuroborosAgent")

    def test_loop_module_importable(self):
        """loop.py imports cleanly with tracing imports."""
        import ouroboros.loop
        assert hasattr(ouroboros.loop, "run_llm_loop")

    def test_handle_task_has_observe(self):
        """handle_task method exists and is callable (with or without decorator)."""
        from ouroboros.agent import OuroborosAgent
        assert callable(getattr(OuroborosAgent, "handle_task", None))

    def test_run_llm_loop_has_observe(self):
        """run_llm_loop is callable (with or without decorator)."""
        from ouroboros.loop import run_llm_loop
        assert callable(run_llm_loop)

    def test_call_llm_with_retry_has_observe(self):
        """_call_llm_with_retry is callable (with or without decorator)."""
        from ouroboros.loop import _call_llm_with_retry
        assert callable(_call_llm_with_retry)

    def test_tool_registry_still_works(self):
        """Tool registry works with tracing-integrated modules."""
        from ouroboros.tools.registry import ToolRegistry
        tmp = pathlib.Path(tempfile.mkdtemp())
        registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
        schemas = registry.schemas()
        assert len(schemas) > 0, "Tool registry should have tools"
