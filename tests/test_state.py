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

def test_update_state_variable(mock_memory):
    args = {"key": "test_key", "value": "test_value"}
    result = update_state_variable(args)
    assert "Working state successfully updated" in result
    
    state_file = mock_memory / "working_state.json"
    state = json.loads(state_file.read_text())
    assert state["test_key"] == "test_value"
