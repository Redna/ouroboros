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
    registry
)


# =============================================================================
# SYSTEM PROMPT CONSTRUCTION TESTS
# =============================================================================

def test_build_static_system_prompt_trunk(mock_memory):
    """Test system prompt construction in Trunk mode."""
    with patch("constants.ROOT_DIR", mock_memory):
        (mock_memory / "CONSTITUTION.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "identity.md").write_text("Identity Content")

        trunk_prompt = build_static_system_prompt(
            is_trunk=True,
            active_tool_specs=[{"function": {"name": "test_tool", "description": "desc"}}]
        )
        assert "Constitution Content" in trunk_prompt
        assert "Identity Content" in trunk_prompt
        assert "GLOBAL ORCHESTRATOR" in trunk_prompt

def test_build_static_system_prompt_branch(mock_memory):
    """Test system prompt construction in Branch mode."""
    with patch("constants.ROOT_DIR", mock_memory):
        (mock_memory / "CONSTITUTION.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "identity.md").write_text("Identity Content")
        branch_prompt = build_static_system_prompt(
            is_trunk=False,
            active_tool_specs=[],
            branch_info={"objective": "Test Objective"}
        )
        assert "Test Objective" in branch_prompt


# =============================================================================
# RECOVERY TOOLS TESTS
# =============================================================================

def test_lazarus_recovery_manual(mock_memory):
    """Test manual system recovery via lazarus tool."""
    with patch("agent_state.append_task_message") as mock_append, \
         patch.object(registry, 'execute') as mock_exec:
        lazarus_recovery("t1", reason="manual trigger")
        # Should append a signal marker and mark the task as complete
        assert mock_append.called
        assert mock_exec.called
        assert mock_exec.call_args[0][0] == "complete_task"


# =============================================================================
# MAIN LOOP TESTS
# =============================================================================

def test_main_loop_iteration(mock_memory):
    """Test a single iteration of the main execution loop."""
    with patch("llm_interface.client.chat.completions.create") as mock_openai, \
         patch("requests.get") as mock_get, \
         patch("time.sleep", side_effect=KeyboardInterrupt("stop loop")), \
         patch("agent_state.load_task_queue", return_value=[{"task_id": "t1", "description": "test"}]):
        
        mock_message = MagicMock()
        mock_message.content = "thinking"
        mock_message.tool_calls = []
        mock_message.model_dump.return_value = {"role": "assistant", "content": "thinking"}
        
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=mock_message)]
        mock_response.usage = MagicMock(total_tokens=100, prompt_tokens=50, completion_tokens=50)
        mock_openai.return_value = mock_response
        
        mock_get.return_value.json.return_value = {"ok": True, "result": []}
        
        with pytest.raises(KeyboardInterrupt):
            main()
        
        assert mock_openai.called


