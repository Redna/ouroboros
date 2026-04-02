"""
Smoke tests for the Ouroboros v5.0 True Seed Architecture.
Each test enforces one v5 invariant; all must pass before any commit.
"""
import inspect
import pathlib

import pytest


# ---------------------------------------------------------------------------
# WP5 Invariants: Singular Timeline Signatures
# ---------------------------------------------------------------------------

def test_seed_agent_imports():
    """Ensure seed_agent is importable without errors."""
    import seed_agent  # noqa: F401
    assert hasattr(seed_agent, "build_static_system_prompt")
    assert hasattr(seed_agent, "_resolve_execution_context")
    assert hasattr(seed_agent, "main")


def test_singular_timeline_signature():
    """build_static_system_prompt no longer accepts is_trunk or branch_info."""
    from seed_agent import build_static_system_prompt
    sig = inspect.signature(build_static_system_prompt)
    params = list(sig.parameters.keys())
    assert "is_trunk" not in params, "is_trunk must be removed (WP5)"
    assert "branch_info" not in params, "branch_info must be removed (WP5)"
    assert "active_tool_specs" in params


def test_resolve_execution_context_returns_singular_stream():
    """_resolve_execution_context must not reference is_trunk or branch_info."""
    from seed_agent import _resolve_execution_context
    params = list(inspect.signature(_resolve_execution_context).parameters.keys())
    assert "state" in params
    assert "queue" in params
    assert "is_trunk" not in params
    assert "branch_info" not in params


# ---------------------------------------------------------------------------
# WP6 Invariants: Accordion fold_context
# ---------------------------------------------------------------------------

def test_fold_context_no_task_id():
    """fold_context schema must only expose 'synthesis' — no task_id or drop_turns."""
    from seed_agent import registry
    props = registry.tools["fold_context"]["params"]["properties"]
    assert "task_id"    not in props, "fold_context must not accept task_id (WP6)"
    assert "drop_turns" not in props, "fold_context must not accept drop_turns (WP6)"
    assert "synthesis"  in props, "fold_context must require synthesis (WP6)"


# ---------------------------------------------------------------------------
# WP7 Invariants: Purge legacy OS-model tools
# ---------------------------------------------------------------------------

def test_banned_tools_absent():
    """suspend_task and update_state_variable must be gone from the registry."""
    from seed_agent import registry
    assert "suspend_task"          not in registry.tools, "suspend_task must be removed (WP7)"
    assert "update_state_variable" not in registry.tools, "update_state_variable must be removed (WP7)"


def test_complete_task_no_task_id():
    """complete_task schema must not expose task_id — it pops the queue front."""
    from seed_agent import registry
    props = registry.tools["complete_task"]["params"]["properties"]
    assert "task_id" not in props, "complete_task must not accept task_id (WP7)"
    assert "synthesis" in props


# ---------------------------------------------------------------------------
# WP8 Invariants: Sticky Note migration
# ---------------------------------------------------------------------------

def test_no_global_trunk_references():
    """'global_trunk' must not appear in comms.py, agent_state.py, or seed_agent.py."""
    root = pathlib.Path(__file__).parent.parent
    for fname in ("comms.py", "agent_state.py", "seed_agent.py"):
        src = (root / fname).read_text(encoding="utf-8")
        assert "global_trunk" not in src, (
            f'"global_trunk" reference found in {fname} — must be purged (WP8)'
        )


# ---------------------------------------------------------------------------
# WP9 Invariants: autonomic_fold / rollback_task_log signatures
# ---------------------------------------------------------------------------

def test_autonomic_fold_no_args():
    """autonomic_fold must take zero arguments (hardcoded to singular_stream)."""
    from agent_state import autonomic_fold
    params = list(inspect.signature(autonomic_fold).parameters.keys())
    assert params == [], f"autonomic_fold must take zero args, got: {params} (WP9)"


def test_autonomic_fold_sets_flag_not_truncates(tmp_path, monkeypatch):
    """autonomic_fold must set force_fold=True in state and NOT physically truncate the log."""
    import json as _json
    import agent_state
    import constants

    # Point memory dir at tmp_path
    monkeypatch.setattr(constants, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(constants, "STATE_PATH", tmp_path / ".agent_state.json")

    # Write a fake singular_stream log with 10 messages
    log = tmp_path / "task_log_singular_stream.jsonl"
    messages = [{"role": ("user" if i % 2 == 0 else "assistant"), "content": f"msg{i}"} for i in range(10)]
    log.write_text("\n".join(_json.dumps(m) for m in messages) + "\n", encoding="utf-8")

    # Write minimal state file
    (tmp_path / ".agent_state.json").write_text(_json.dumps({}), encoding="utf-8")

    agent_state.autonomic_fold()

    # Flag must be set
    state = _json.loads((tmp_path / ".agent_state.json").read_text())
    assert state.get("force_fold") is True, "autonomic_fold must set force_fold=True"

    # Log must NOT have been truncated
    lines_after = [l for l in log.read_text().splitlines() if l.strip()]
    assert len(lines_after) == 11, (  # 10 original + 1 emergency notice appended
        f"autonomic_fold must not truncate the log; expected 11 lines, got {len(lines_after)}"
    )


def test_rollback_task_log_no_args():
    """rollback_task_log must take zero arguments (hardcoded to singular_stream)."""
    from agent_state import rollback_task_log
    params = list(inspect.signature(rollback_task_log).parameters.keys())
    assert params == [], f"rollback_task_log must take zero args, got: {params} (WP9)"


# ---------------------------------------------------------------------------
# WP10 Invariants: enforce_context_limits / update_global_metrics signatures
# ---------------------------------------------------------------------------

def test_enforce_context_limits_no_task_id():
    """enforce_context_limits must not accept task_id."""
    from agent_state import enforce_context_limits
    params = list(inspect.signature(enforce_context_limits).parameters.keys())
    assert "task_id" not in params, f"enforce_context_limits must not accept task_id (WP10)"
    assert "state" in params
    assert "queue" in params


def test_update_global_metrics_no_task_id():
    """update_global_metrics must not accept task_id."""
    from agent_state import update_global_metrics
    params = list(inspect.signature(update_global_metrics).parameters.keys())
    assert "task_id" not in params, f"update_global_metrics must not accept task_id (WP10)"


def test_context_thresholds_in_constants():
    """CONTEXT_BREACH/LAST_GASP/WARN_THRESHOLD must exist as floats in constants."""
    import constants
    assert isinstance(constants.CONTEXT_BREACH_THRESHOLD,    float), "CONTEXT_BREACH_THRESHOLD missing (WP10)"
    assert isinstance(constants.CONTEXT_LAST_GASP_THRESHOLD, float), "CONTEXT_LAST_GASP_THRESHOLD missing (WP10)"
    assert isinstance(constants.CONTEXT_WARN_THRESHOLD,      float), "CONTEXT_WARN_THRESHOLD missing (WP10)"


# ---------------------------------------------------------------------------
# WP11 Invariants: Purge WORKING_STATE_PATH
# ---------------------------------------------------------------------------

def test_no_working_state_path_in_constants():
    """WORKING_STATE_PATH must be gone from constants.py."""
    root = pathlib.Path(__file__).parent.parent
    src = (root / "constants.py").read_text(encoding="utf-8")
    assert "WORKING_STATE_PATH" not in src, (
        '"WORKING_STATE_PATH" still present in constants.py — must be purged (WP11)'
    )


def test_push_task_no_parent_task_id():
    """push_task schema must not expose parent_task_id — branches are gone."""
    from seed_agent import registry
    props = registry.tools["push_task"]["params"]["properties"]
    assert "parent_task_id" not in props, "push_task must not accept parent_task_id (WP11)"


# ---------------------------------------------------------------------------
# WP6 + HUD format verification
# ---------------------------------------------------------------------------

def test_hud_format():
    """build_dynamic_telemetry_message must output exactly '[HUD | Context: X% | Queue: Y]'."""
    from seed_agent import build_dynamic_telemetry_message
    import constants
    # Use an exact multiple of CONTEXT_WINDOW so int() truncation is unambiguous
    context_size = constants.CONTEXT_WINDOW // 2   # exactly 50%
    state = {"last_context_size": context_size}
    queue = [{}, {}]
    hud = build_dynamic_telemetry_message(state, queue)
    assert hud == "[HUD | Context: 50% | Queue: 2]", (
        f"HUD format mismatch. Got: {hud!r}"
    )
