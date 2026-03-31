import json
from unittest.mock import patch
from llm_interface import redact_secrets
from seed_agent import update_state_variable

def test_redact_secrets():
    with patch("constants.TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
        text = "My token is 123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        redacted = redact_secrets(text)
        assert "[REDACTED]" in redacted or "[REDACTED_TOKEN]" in redacted
        assert "123456789:" not in redacted

from agent_state import enforce_context_limits
import constants

def test_enforce_context_limits_branch_exhaustion(mock_memory):
    """Test that a branch hitting limits signals a breach."""
    state = {"last_context_size": int(constants.CONTEXT_WINDOW * 0.92)}
    queue = [{"task_id": "b1", "priority": 1, "turn_count": 5}]

    with patch("agent_state.append_task_message"):
        new_q, status = enforce_context_limits(state, queue, "b1", is_trunk=False)
        assert status == "LAST_GASP"
def test_enforce_context_limits_trunk_amnesia(mock_memory):
    """Test that Trunk hitting limits signals amnesia."""
    # Hit turn limit
    state = {"last_context_size": 1000}
    queue = [{"task_id": "global_trunk", "priority": 1, "turn_count": 51}]
    
    new_q, status = enforce_context_limits(state, queue, "global_trunk", is_trunk=True)
    assert status == "BREACH"
