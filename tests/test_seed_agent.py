import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from seed_agent import (
    add_cognitive_load, 
    handle_update_state, 
    redact_secrets, 
    load_state, 
    save_state,
    WORKING_STATE_PATH,
    STATE_PATH
)

@pytest.fixture
def mock_state_path(tmp_path):
    with patch("seed_agent.STATE_PATH", tmp_path / ".agent_state.json") as m:
        yield m

@pytest.fixture
def mock_working_state_path(tmp_path):
    with patch("seed_agent.WORKING_STATE_PATH", tmp_path / "working_state.json") as m:
        yield m

def test_redact_secrets():
    with patch("seed_agent.TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
        text = "My token is 123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        redacted = redact_secrets(text)
        assert "[REDACTED]" in redacted
        assert "123456789:" not in redacted

def test_add_cognitive_load(mock_state_path):
    # Initial state
    mock_state_path.write_text(json.dumps({"cognitive_load": 10}))
    
    add_cognitive_load(20)
    
    state = json.loads(mock_state_path.read_text())
    assert state["cognitive_load"] == 30

def test_handle_update_state(mock_working_state_path):
    # Test updating a new state
    args = {"key": "test_key", "value": "test_value"}
    result = handle_update_state(args)
    
    assert "Working state successfully updated" in result
    state = json.loads(mock_working_state_path.read_text())
    assert state["test_key"] == "test_value"
    
    # Test updating an existing state
    args = {"key": "another_key", "value": 123}
    handle_update_state(args)
    state = json.loads(mock_working_state_path.read_text())
    assert state["test_key"] == "test_value"
    assert state["another_key"] == 123

def test_handle_update_state_invalid_args():
    assert "Error" in handle_update_state({"key": "only_key"})
    assert "Error" in handle_update_state({"value": "only_value"})
