import json
import pytest
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from seed_agent import (
    handle_update_state, 
    redact_secrets, 
    load_state, 
    save_state,
    add_cognitive_load,
    handle_bash,
    handle_write,
    handle_read_file_tool,
    handle_push_task,
    handle_telegram,
    handle_web_search,
    load_task_messages,
    append_task_message,
    WORKING_STATE_PATH,
    MEMORY_DIR,
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
    monkeypatch.setattr("seed_agent.TASK_QUEUE_PATH", memory_dir / "task_queue.json")
    monkeypatch.setattr("seed_agent.CHAT_HISTORY_PATH", memory_dir / "chat_history.json")
    monkeypatch.setattr("seed_agent.ARCHIVE_PATH", memory_dir / "global_biography.md")
    
    # Initialize some required files
    (memory_dir / ".agent_state.json").write_text(json.dumps({"offset": 0, "cognitive_load": 0}))
    (memory_dir / "task_queue.json").write_text("[]")
    (memory_dir / "working_state.json").write_text("{}")
    
    return memory_dir

def test_redact_secrets():
    with patch("seed_agent.TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
        text = "My token is 123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        redacted = redact_secrets(text)
        assert "[REDACTED]" in redacted or "[REDACTED_TOKEN]" in redacted
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
    assert "Success" in result
    assert path.read_text() == content

def test_handle_read_file_tool(mock_memory):
    test_file = mock_memory / "read_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    result = handle_read_file_tool({"path": str(test_file)})
    assert "line1\nline2\nline3\nline4" in result
    
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

def test_build_static_system_prompt(mock_memory):
    from seed_agent import build_static_system_prompt
    with patch("seed_agent.ROOT_DIR", mock_memory):
        (mock_memory / "BIBLE.md").write_text("Constitution Content")
        (mock_memory / "soul").mkdir(exist_ok=True)
        (mock_memory / "soul" / "identity.md").write_text("Identity Content")

        # Test Trunk mode
        trunk_prompt = build_static_system_prompt(is_trunk=True, active_tool_specs=[{"function": {"name": "test_tool", "description": "desc"}}])
        assert "Constitution Content" in trunk_prompt
        assert "Identity Content" in trunk_prompt
        assert "GLOBAL TRUNK" in trunk_prompt
        assert "test_tool" in trunk_prompt

        # Test Branch mode
        branch_prompt = build_static_system_prompt(is_trunk=False, active_tool_specs=[], branch_info={"objective": "Test Objective"})
        assert "EXECUTION BRANCH" in branch_prompt
        assert "Test Objective" in branch_prompt
def test_tool_registry_buckets():
    from seed_agent import ToolRegistry
    from unittest.mock import MagicMock

    reg = ToolRegistry()
    handler = MagicMock(return_value="success")

    # Register tools in different buckets
    reg.register("global_tool", "desc", {}, handler, bucket="global")
    reg.register("fs_tool", "desc", {}, handler, bucket="filesystem")

    # Test bucket filtering
    global_tools = reg.get_names(allowed_buckets=["global"])
    assert "global_tool" in global_tools
    assert "fs_tool" not in global_tools

    # Test multiple buckets
    all_tools = reg.get_names(allowed_buckets=["global", "filesystem"])
    assert "global_tool" in all_tools
    assert "fs_tool" in all_tools

    # Test execution
    assert reg.execute("global_tool", {"arg": 1}) == "success"
    handler.assert_called_once_with({"arg": 1})
def test_lazarus_recovery(mock_memory):
    from seed_agent import lazarus_recovery, registry
    
    with patch.object(registry, 'execute') as mock_exec:
        lazarus_recovery("t1", reason="test loop")
        # Should call compress_memory_block and mark_task_complete
        assert mock_exec.call_count >= 2

def test_main_loop_iteration(mock_memory):
    from seed_agent import main
    
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

def test_handle_fork_execution(mock_memory):
    from seed_agent import handle_fork_execution, load_state
    
    args = {
        "task_id": "task_fork_test_1", 
        "objective": "Rewrite database schema", 
        "tool_buckets": ["filesystem"]
    }
    
    result = handle_fork_execution(args)
    
    # Check the return signal
    assert result == "SYSTEM_SIGNAL_FORK:task_fork_test_1"
    
    # Verify the state mutated correctly
    state = load_state()
    assert state["active_branch"]["task_id"] == "task_fork_test_1"
    assert state["active_branch"]["objective"] == "Rewrite database schema"
    assert state["active_branch"]["tool_buckets"] == ["filesystem"]

def test_handle_merge_and_return(mock_memory):
    from seed_agent import handle_merge_and_return, save_state, load_state
    import json
    
    # Setup an active branch first
    save_state({
        "active_branch": {
            "task_id": "task_merge_test_1", 
            "objective": "test", 
            "tool_buckets": []
        }
    })
    
    args = {
        "status": "SUSPENDED", 
        "synthesis_summary": "I hit a blocker", 
        "partial_state": "Files downloaded, but not parsed"
    }
    
    result = handle_merge_and_return(args)
    
    # Check the state is cleared
    state = load_state()
    assert state.get("active_branch") is None
    
    # Check the payload formatting
    assert result.startswith("SYSTEM_SIGNAL_MERGE:")
    payload_str = result.split(":", 1)[1]
    payload = json.loads(payload_str)
    
    assert payload["status"] == "SUSPENDED"
    assert payload["task_id"] == "task_merge_test_1"
    assert payload["summary"] == "I hit a blocker"
    assert payload["partial_state"] == "Files downloaded, but not parsed"

def test_enforce_interrupt_yield():
    from seed_agent import enforce_interrupt_yield
    
    queue_normal = [{"task_id": "t1", "priority": 1}]
    queue_interrupt = [{"task_id": "t1", "priority": 1}, {"task_id": "t2", "priority": 999}]
    
    messages = [{"role": "user", "content": "Doing regular work."}]
    
    # Test 1: No interrupt in queue
    result_normal = enforce_interrupt_yield("task_1", queue_normal, messages)
    assert len(result_normal) == 1
    
    # Test 2: Interrupt in queue injects message
    result_interrupt = enforce_interrupt_yield("task_1", queue_interrupt, messages)
    assert len(result_interrupt) == 2
    assert "URGENT PRIORITY 999 INTERRUPT" in result_interrupt[1]["content"]
    
    # Test 3: Scrubbing old interrupts
    messages_with_old_interrupt = [
        {"role": "user", "content": "Doing regular work."},
        {"role": "user", "content": "[SYSTEM OVERRIDE: URGENT PRIORITY 999 INTERRUPT IN GLOBAL QUEUE. You must suspend...]"}
    ]
    
    result_scrubbed = enforce_interrupt_yield("task_1", queue_interrupt, messages_with_old_interrupt)
    # It should strip the old one and append the new one, resulting in exactly 2 messages
    assert len(result_scrubbed) == 2

