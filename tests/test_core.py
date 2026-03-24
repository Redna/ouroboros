"""
Test suite for core Ouroboros functionality.
Covers system prompts, recovery mechanisms, main loop, and interrupt handling.
"""
import pytest
from unittest.mock import patch, MagicMock
from seed_agent import (
    build_static_system_prompt,
    lazarus_recovery,
    main,
    enforce_interrupt_yield,
    registry
)


# =============================================================================
# SYSTEM PROMPT CONSTRUCTION TESTS
# =============================================================================

def test_build_static_system_prompt_trunk(mock_memory):
    """Test system prompt construction in Trunk mode."""
    with patch("seed_agent.ROOT_DIR", mock_memory):
        (mock_memory / "CONSTITUTION.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "soul" / "identity.md").write_text("Identity Content")

        trunk_prompt = build_static_system_prompt(
            is_trunk=True,
            active_tool_specs=[{"function": {"name": "test_tool", "description": "desc"}}]
        )
        assert "Constitution Content" in trunk_prompt
        assert "Identity Content" in trunk_prompt
        assert "GLOBAL TRUNK" in trunk_prompt
        assert "test_tool" in trunk_prompt


def test_build_static_system_prompt_branch(mock_memory):
    """Test system prompt construction in Branch mode."""
    with patch("seed_agent.ROOT_DIR", mock_memory):
        (mock_memory / "CONSTITUTION.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "soul" / "identity.md").write_text("Identity Content")

        branch_prompt = build_static_system_prompt(
            is_trunk=False,
            active_tool_specs=[],
            branch_info={"objective": "Test Objective"}
        )
        assert "EXECUTION BRANCH" in branch_prompt
        assert "Test Objective" in branch_prompt


# =============================================================================
# LAZARUS RECOVERY TESTS
# =============================================================================

def test_lazarus_recovery(mock_memory):
    """Test system recovery after loop failure."""
    with patch.object(registry, 'execute') as mock_exec:
        lazarus_recovery("t1", reason="test loop")
        # Should call compress_memory_block and mark_task_complete
        assert mock_exec.call_count >= 2


# =============================================================================
# MAIN LOOP TESTS
# =============================================================================

def test_main_loop_iteration(mock_memory):
    """Test a single iteration of the main execution loop."""
    with patch("seed_agent.client.chat.completions.create") as mock_openai, \
         patch("requests.get") as mock_get, \
         patch("time.sleep", side_effect=KeyboardInterrupt("stop loop")), \
         patch("seed_agent.load_task_queue", return_value=[{"task_id": "t1", "description": "test"}]):
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="thinking", tool_calls=[]))]
        mock_response.usage = MagicMock(total_tokens=100, prompt_tokens=50, completion_tokens=50)
        mock_openai.return_value = mock_response
        
        mock_get.return_value.json.return_value = {"ok": True, "result": []}
        
        with pytest.raises(KeyboardInterrupt):
            main()
        
        assert mock_openai.called


# =============================================================================
# INTERRUPT HANDLING TESTS
# =============================================================================

def test_enforce_interrupt_yield_no_interrupt():
    """Test that normal messages are not modified when no interrupt exists."""
    queue_normal = [{"task_id": "t1", "priority": 1}]
    messages = [{"role": "user", "content": "Doing regular work."}]
    
    result_normal = enforce_interrupt_yield("task_1", queue_normal, messages)
    assert len(result_normal) == 1


def test_enforce_interrupt_yield_inject():
    """Test that interrupt message is injected when P999 task exists."""
    queue_interrupt = [
        {"task_id": "t1", "priority": 1},
        {"task_id": "t2", "priority": 999}
    ]
    messages = [{"role": "user", "content": "Doing regular work."}]
    
    result_interrupt = enforce_interrupt_yield("task_1", queue_interrupt, messages)
    assert len(result_interrupt) == 2
    assert "URGENT PRIORITY 999 INTERRUPT" in result_interrupt[1]["content"]


def test_enforce_interrupt_yield_scrub():
    """Test that old interrupt messages are scrubbed before injecting new ones."""
    queue_interrupt = [
        {"task_id": "t1", "priority": 1},
        {"task_id": "t2", "priority": 999}
    ]
    messages_with_old_interrupt = [
        {"role": "user", "content": "Doing regular work."},
        {"role": "user", "content": "[SYSTEM OVERRIDE: URGENT PRIORITY 999 INTERRUPT IN GLOBAL QUEUE. You must suspend...]"}
    ]
    
    result_scrubbed = enforce_interrupt_yield("task_1", queue_interrupt, messages_with_old_interrupt)
    # It should strip the old one and append the new one, resulting in exactly 2 messages
    assert len(result_scrubbed) == 2
