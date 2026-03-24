from unittest.mock import patch, MagicMock
from seed_agent import handle_bash, handle_write, handle_read_file_tool, handle_telegram, handle_web_search, handle_store_insight
import json

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
    result = handle_store_insight({"insight": "Deep thought", "category": "Mind"})
    assert "stored" in result
    assert "Deep thought" in (mock_memory / "insights.md").read_text()

def test_tool_registry_buckets():
    from seed_agent import ToolRegistry

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
