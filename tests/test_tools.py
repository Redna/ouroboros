"""
Test suite for individual tool handlers.
Covers bash execution, file operations, communication tools, and tool registry.
"""
from unittest.mock import patch, MagicMock
from seed_agent import (
    bash_command,
    write_file,
    patch_file,
    read_file_tool,
    generate_repo_map,
    fold_context,
    send_telegram_message,
    web_search,
    store_memory_insight,
    ToolRegistry
)
import json

def test_fold_context(mock_memory):
    """Test sawtooth context folding logic."""
    task_id = "test_fold"
    log_path = mock_memory / f"task_log_{task_id}.jsonl"
    
    # Setup initial log with 5 messages
    initial_msgs = [
        {"role": "user", "content": "Start"},
        {"role": "assistant", "content": "Thought 1"},
        {"role": "tool", "content": "Result 1"},
        {"role": "assistant", "content": "Thought 2"},
        {"role": "tool", "content": "Result 2"}
    ]
    with open(log_path, "w") as f:
        for m in initial_msgs:
            f.write(json.dumps(m) + "\n")
            
    # Fold last 2 steps
    result = fold_context({
        "task_id": task_id,
        "synthesis": "Successfully calculated X.",
        "steps_to_drop": 2
    })
    
    assert "successfully folded" in result
    
    # Verify log content
    with open(log_path, "r") as f:
        final_msgs = [json.loads(line) for line in f if line.strip()]
        
    # Should have 3 preserved + 1 synthesis = 4 messages
    assert len(final_msgs) == 4
    assert final_msgs[0]["content"] == "Start"
    assert "FOCUS SYNTHESIS" in final_msgs[-1]["content"]
    assert "Successfully calculated X." in final_msgs[-1]["content"]


def test_generate_repo_map(mock_memory):
    """Test repository mapping with Tree-sitter."""
    # Create a dummy python file in mock_memory
    test_dir = mock_memory / "test_app"
    test_dir.mkdir(parents=True, exist_ok=True)
    py_file = test_dir / "dummy.py"
    py_file.write_text("""
class MyClass:
    def my_method(self):
        pass

def global_function():
    pass
""")
    
    with patch("constants.ROOT_DIR", mock_memory):
        result = generate_repo_map({"path": str(test_dir)})
        
        assert "dummy.py" in result
        assert "class MyClass:" in result
        assert "def my_method(...):" in result
        assert "def global_function(...):" in result


def test_bash_command_success():
    """Test successful bash command execution."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="hello", stderr="", returncode=0)
        result = bash_command({"command": "echo hello"})
        assert "hello" in result


def test_bash_command_failure():
    """Test bash command with non-zero exit code."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="command not found", returncode=1)
        result = bash_command({"command": "nonexistent_command"})
        assert "command not found" in result

def test_write_file(mock_memory):
    """Test file writing functionality."""
    path = mock_memory / "test.txt"
    content = "hello world"
    result = write_file({"path": str(path), "content": content})
    assert "Success" in result
    assert path.read_text() == content

def test_patch_file_replace(mock_memory):
    """Test patching a file by replacing code block."""
    test_file = mock_memory / "patch_test.txt"
    test_file.write_text("line1\nline2 \nline3\nline4")
    
    result = patch_file({
        "path": str(test_file),
        "search_text": "line2\nline3",
        "replace_text": "replaced_line"
    })
    assert "Success" in result
    # Resulting file will have line2 and line3 replaced by replaced_line
    # Note that norm_content.replace(norm_search, replace_text)
    # norm_content for "line1\nline2 \nline3\nline4" is "line1\nline2\nline3\nline4"
    # replaced with "replaced_line" gives "line1\nreplaced_line\nline4"
    assert test_file.read_text() == "line1\nreplaced_line\nline4"

def test_patch_file_delete(mock_memory):
    """Test patching a file by deleting block."""
    test_file = mock_memory / "patch_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    result = patch_file({
        "path": str(test_file),
        "search_text": "line2\nline3",
        "replace_text": ""
    })
    assert "Success" in result
    # "line1\nline2\nline3\nline4".replace("line2\nline3", "") -> "line1\n\nline4"
    assert test_file.read_text() == "line1\n\nline4"

def test_read_file_tool_full(mock_memory):
    """Test reading entire file."""
    test_file = mock_memory / "read_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    result = read_file_tool({"path": str(test_file)})
    assert "line1\nline2\nline3\nline4" in result

def test_read_file_tool_range(mock_memory):
    """Test reading specific line range."""
    test_file = mock_memory / "read_test.txt"
    test_file.write_text("line1\nline2\nline3\nline4")
    
    result = read_file_tool({"path": str(test_file), "start_line": 2, "end_line": 3})
    assert "line2\nline3" in result
    assert "line1" not in result

def test_send_telegram_message(mock_memory):
    """Test telegram message sending."""
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        with patch("constants.TELEGRAM_BOT_TOKEN", "fake_token"):
            result = send_telegram_message({"text": "test msg", "chat_id": 123})
            assert "successfully" in result

def test_web_search(mock_memory):
    """Test web search functionality."""
    with patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "results": [
                {"title": "T1", "url": "U1", "content": "C1"},
                {"title": "T2", "url": "U2", "content": "C2"}
            ]
        }
        with patch("constants.SEARXNG_URL", "http://fake_searxng"):
            result = web_search({"query": "test"})
            assert "T1" in result
            assert "U1" in result

def test_store_memory_insight(mock_memory):
    """Test storing insights."""
    result = store_memory_insight({"insight": "Deep thought", "category": "Mind"})
    assert "stored" in result
    assert "Deep thought" in (mock_memory / "insights.md").read_text()

def test_tool_registry_buckets():
    """Test tool registry bucket filtering."""
    reg = ToolRegistry()
    
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

def test_tool_registry_execution():
    """Test tool registry execution."""
    reg = ToolRegistry()
    handler = MagicMock(return_value="success")
    handler.__name__ = "test_tool" # Mock the function name

    # Decorate the mock handler
    decorated_handler = reg.tool("desc", {})(handler)
    
    result = reg.execute("test_tool", {"arg": 1})
    assert result == "success"
    handler.assert_called_once_with({"arg": 1})
