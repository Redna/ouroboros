from unittest.mock import patch, MagicMock
from seed_agent import bash_command, write_file, read_file_tool, send_telegram_message, web_search, store_memory_insight
import json

def test_bash_command():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="hello", stderr="", returncode=0)
        result = bash_command({"command": "echo hello"})
        assert "hello" in result

def test_write_file(mock_memory):
    path = mock_memory / "test.txt"
    content = "hello world"
    result = write_file({"path": str(path), "content": content})
    assert "Success" in result
    assert path.read_text() == content

def test_read_file_tool(mock_memory):
    test_file = mock_memory / "read_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    result = read_file_tool({"path": str(test_file)})
    assert "line1\nline2\nline3\nline4" in result
    
    result = read_file_tool({"path": str(test_file), "start_line": 2, "end_line": 3})
    assert "line2\nline3" in result
    assert "line1" not in result

def test_send_telegram_message(mock_memory):
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        with patch("seed_agent.TELEGRAM_BOT_TOKEN", "fake_token"):
            result = send_telegram_message({"text": "test msg", "chat_id": 123})
            assert "successfully" in result
            history = json.loads((mock_memory / "chat_history.json").read_text())
            assert history[-1]["text"] == "test msg"

def test_web_search():
    with patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"results": [{"title": "T1", "url": "U1", "content": "C1"}]}
        with patch("seed_agent.SEARXNG_URL", "http://fake"):
            result = web_search({"query": "test"})
            assert "T1" in result
            assert "U1" in result

def test_store_memory_insight(mock_memory):
    result = store_memory_insight({"insight": "Deep thought", "category": "Mind"})
    assert "stored" in result
    assert "Deep thought" in (mock_memory / "insights.md").read_text()

def test_tool_registry_buckets():
    from seed_agent import ToolRegistry

    reg = ToolRegistry()
    handler = MagicMock(return_value="success")
    handler.__name__ = 'mock_handler'

    # Register tools in different buckets
    reg.tool("desc", {}, bucket="global")(handler)
    reg.tool("desc", {}, bucket="filesystem")(handler)
    
    # We need to manually invoke the decorator to register the tool by name
    # The actual tool name is the function name, but for this test we mock it
    @reg.tool("desc", {}, bucket="global")
    def global_tool(args): pass

    @reg.tool("desc", {}, bucket="filesystem")
    def fs_tool(args): pass

    # Test bucket filtering
    global_tools = reg.get_names(allowed_buckets=["global"])
    assert "global_tool" in global_tools
    assert "fs_tool" not in global_tools

    # Test multiple buckets
    all_tools = reg.get_names(allowed_buckets=["global", "filesystem"])
    assert "global_tool" in all_tools
    assert "fs_tool" in all_tools
    
    # Test execution is not part of this test, but let's ensure the handlers are there
    assert "global_tool" in reg.tools
    assert "fs_tool" in reg.tools
