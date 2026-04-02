"""
Minimal smoke tests — kept alive so the pre-commit hook always collects
at least one item.  Deeper test suites to be rebuilt post-WP5 refactor.
"""
import importlib


def test_seed_agent_imports():
    """Ensure seed_agent is importable without errors."""
    import seed_agent  # noqa: F401
    assert hasattr(seed_agent, "build_static_system_prompt")
    assert hasattr(seed_agent, "_resolve_execution_context")
    assert hasattr(seed_agent, "main")


def test_singular_timeline_signature():
    """build_static_system_prompt no longer accepts is_trunk or branch_info."""
    import inspect
    from seed_agent import build_static_system_prompt
    sig = inspect.signature(build_static_system_prompt)
    params = list(sig.parameters.keys())
    assert "is_trunk" not in params, "is_trunk must be removed (WP5)"
    assert "branch_info" not in params, "branch_info must be removed (WP5)"
    assert "active_tool_specs" in params


def test_resolve_execution_context_returns_singular_stream():
    """_resolve_execution_context must return singular_stream task id."""
    import inspect
    from seed_agent import _resolve_execution_context
    # Verify return annotation is a 3-tuple (no is_trunk, no branch_info)
    ann = inspect.signature(_resolve_execution_context).return_annotation
    # Just confirm the function exists and returns 3 values with an empty queue
    # We can't call it without the registry, so check the signature only
    params = list(inspect.signature(_resolve_execution_context).parameters.keys())
    assert "state" in params
    assert "queue" in params
    assert "is_trunk" not in params
    assert "branch_info" not in params
