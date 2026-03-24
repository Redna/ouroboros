import pytest
import json
from unittest.mock import patch

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
