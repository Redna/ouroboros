import json
import pytest
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from seed_agent import (
    add_cognitive_load, 
    handle_update_state, 
    redact_secrets, 
    load_state, 
    save_state,
    handle_bash,
    handle_write,
    handle_read_file_tool,
    handle_push_task,
    handle_clear_inbox,
    handle_telegram,
    handle_web_search,
    load_task_messages,
    append_task_message,
    WORKING_STATE_PATH,
    STATE_PATH,
    MEMORY_DIR,
    INBOX_PATH,
    TASK_QUEUE_PATH
)

@pytest.fixture
def mock_memory(tmp_path, monkeypatch):
    # Setup temporary directory structure
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    
    # Mock all paths in seed_agent module
    monkeypatch.setattr("seed_agent.MEMORY_DIR", memory_dir)
    monkeypatch.setattr("seed_agent.STATE_PATH", memory_dir / ".agent_state.json")
    monkeypatch.setattr("seed_agent.WORKING_STATE_PATH", memory_dir / "working_state.json")
    monkeypatch.setattr("seed_agent.INBOX_PATH", memory_dir / "inbox.json")
    monkeypatch.setattr("seed_agent.TASK_QUEUE_PATH", memory_dir / "task_queue.json")
    monkeypatch.setattr("seed_agent.CHAT_HISTORY_PATH", memory_dir / "chat_history.json")
    monkeypatch.setattr("seed_agent.ARCHIVE_PATH", memory_dir / "global_biography.md")
    
    # Initialize some required files
    (memory_dir / ".agent_state.json").write_text(json.dumps({"offset": 0}))
    (memory_dir / "task_queue.json").write_text("[]")
    (memory_dir / "inbox.json").write_text("[]")
    (memory_dir / "working_state.json").write_text("{}")
    
    return memory_dir

def test_redact_secrets():
    with patch("seed_agent.TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
        text = "My token is 123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        redacted = redact_secrets(text)
        assert "[REDACTED]" in redacted
        assert "123456789:" not in redacted

def test_add_cognitive_load(mock_memory):
    save_state({"cognitive_load": 10})
    add_cognitive_load(20)
    state = load_state()
    assert state["cognitive_load"] == 30

def test_handle_update_state(mock_memory):
    args = {"key": "test_key", "value": "test_value"}
    result = handle_update_state(args)
    assert "Working state successfully updated" in result
    
    # Read from the mocked path directly
    state_file = mock_memory / "working_state.json"
    state = json.loads(state_file.read_text())
    assert state["test_key"] == "test_value"

def test_handle_bash():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="hello", stderr="", returncode=0)
        result = handle_bash({"command": "echo hello"})
        assert "hello" in result

def test_handle_write(mock_memory):
    path = mock_memory / "test.txt"
    content = "hello world"
    result = handle_write({"path": str(path), "content": content})
    assert "Wrote test.txt" in result
    assert path.read_text() == content

def test_handle_read_file_tool(mock_memory):
    test_file = mock_memory / "read_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    # Test reading full file
    result = handle_read_file_tool({"path": str(test_file)})
    assert "line1\nline2\nline3\nline4" in result
    
    # Test line range
    result = handle_read_file_tool({"path": str(test_file), "start_line": 2, "end_line": 3})
    assert "line2\nline3" in result
    assert "line1" not in result

def test_handle_push_task(mock_memory):
    result = handle_push_task({"description": "new task", "priority": 2})
    assert "Queued task_" in result
    
    queue_file = mock_memory / "task_queue.json"
    queue = json.loads(queue_file.read_text())
    assert len(queue) == 1
    assert queue[0]["description"] == "new task"

def test_handle_clear_inbox(mock_memory):
    inbox_file = mock_memory / "inbox.json"
    inbox_file.write_text(json.dumps([{"text": "hi", "chat_id": 123}]))
    triage_log = mock_memory / "task_log_triage.jsonl"
    triage_log.write_text("{}")
    
    handle_clear_inbox({})
    assert json.loads(inbox_file.read_text()) == []
    assert not triage_log.exists()

def test_handle_telegram(mock_memory):
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        with patch("seed_agent.TELEGRAM_BOT_TOKEN", "fake_token"):
            result = handle_telegram({"text": "test msg", "chat_id": 123})
            assert "successfully" in result
            history = json.loads((mock_memory / "chat_history.json").read_text())
            assert history[-1]["text"] == "test msg"

def test_handle_web_search():
    with patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"results": [{"title": "T1", "url": "U1", "content": "C1"}]}
        with patch("seed_agent.SEARXNG_URL", "http://fake"):
            result = handle_web_search({"query": "test"})
            assert "T1" in result
            assert "U1" in result

def test_handle_store_insight(mock_memory):
    from seed_agent import handle_store_insight
    result = handle_store_insight({"insight": "Deep thought", "category": "Mind"})
    assert "stored" in result
    assert "Deep thought" in (mock_memory / "insights.md").read_text()

def test_is_high_value_message():
    from seed_agent import is_high_value_message
    
    val, cat, summ = is_high_value_message({"text": "I am becoming a self-creating entity", "role": "Ouroboros"})
    assert val is True
    assert cat == "identity"
    
    val, cat, summ = is_high_value_message({"text": "just chilling", "role": "User"})
    assert val is False

def test_archive_chat_history(mock_memory):
    from seed_agent import archive_chat_history
    history_file = mock_memory / "chat_history.json"
    history_file.write_text(json.dumps([
        {"role": "Ouroboros", "text": "I am becoming a self-creating entity"}, # identity
        {"role": "Ouroboros", "text": "I have achieved a major breakthrough in my logic."}, # breakthrough
        {"role": "Ouroboros", "text": "Hi"},
        {"role": "User", "text": "How are you?"},
        {"role": "Ouroboros", "text": "I am fine."},
        {"role": "User", "text": "Ok"},
        {"role": "User", "text": "1"}, {"role": "User", "text": "2"},
        {"role": "User", "text": "3"}, {"role": "User", "text": "4"},
        {"role": "User", "text": "5"}, {"role": "User", "text": "6"}
    ]))
    
    result = archive_chat_history()
    assert result["insights_stored"] >= 2
    
    # Check history reduction
    new_history = json.loads(history_file.read_text())
    assert len(new_history) < 11

def test_build_static_system_prompt(mock_memory):
    from seed_agent import build_static_system_prompt
    with patch("seed_agent.ROOT_DIR", mock_memory):
        (mock_memory / "BIBLE.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "soul" / "identity.md").write_text("Identity Content")
        
        prompt = build_static_system_prompt("EXECUTION", [{"function": {"name": "test_tool", "description": "desc"}}])
        assert "Constitution Content" in prompt
        assert "Identity Content" in prompt
        assert "EXECUTION" in prompt
        assert "test_tool" in prompt

def test_tool_registry():
    from seed_agent import ToolRegistry
    reg = ToolRegistry()
    handler = MagicMock(return_value="success")
    reg.register("test", "desc", {}, handler)
    
    assert "test" in reg.get_names()
    assert reg.execute("test", {"arg": 1}) == "success"
    handler.assert_called_once_with({"arg": 1})
    assert "not found" in reg.execute("nonexistent", {})

def test_lazarus_recovery(mock_memory):
    from seed_agent import lazarus_recovery
    with patch("subprocess.run") as mock_run:
        lazarus_recovery(reason="test loop")
        # Should call git reset and git clean
        assert mock_run.call_count >= 2

def test_main_loop_iteration(mock_memory):
    from seed_agent import main
    
    # Mocking external dependencies
    with patch("seed_agent.client.chat.completions.create") as mock_openai, \
         patch("requests.get") as mock_get, \
         patch("time.sleep", side_effect=InterruptedError("stop loop")), \
         patch("seed_agent.load_inbox", return_value=[]), \
         patch("seed_agent.load_task_queue", return_value=[{"task_id": "t1", "description": "test"}]):
        
        # Mocking OpenAI response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="thinking", tool_calls=[]))]
        mock_response.usage = MagicMock(total_tokens=100, prompt_tokens=50, completion_tokens=50)
        mock_openai.return_value = mock_response
        
        mock_get.return_value.json.return_value = {"ok": True, "result": []}
        
        with pytest.raises(InterruptedError):
            main()
        
        # Verify that OpenAI was called at least once
        assert mock_openai.called
