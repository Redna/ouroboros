import pytest
import json
from unittest.mock import patch

@pytest.fixture
def mock_memory(tmp_path, monkeypatch):
    # Setup temporary directory structure
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    
    # Mock all paths in constants module
    monkeypatch.setattr("constants.MEMORY_DIR", memory_dir)
    monkeypatch.setattr("constants.STATE_PATH", memory_dir / ".agent_state.json")
    monkeypatch.setattr("constants.WORKING_STATE_PATH", memory_dir / "working_state.json")
    monkeypatch.setattr("constants.TASK_QUEUE_PATH", memory_dir / "task_queue.json")
    monkeypatch.setattr("constants.CHAT_HISTORY_PATH", memory_dir / "chat_history.json")
    monkeypatch.setattr("constants.MEMORY_STORE_PATH", memory_dir / "agent_memory.json")
    monkeypatch.setattr("constants.TASK_ARCHIVE_PATH", memory_dir / "task_archive.jsonl")
    monkeypatch.setattr("constants.MEMORY_MAX_ENTRIES", 50)
    monkeypatch.setattr("constants.SCHEDULED_TASKS_PATH", memory_dir / "scheduled_tasks.json")
    monkeypatch.setattr("constants.LEDGER_FILE", memory_dir / "financial_ledger.json")
    
    # Initialize some required files
    (memory_dir / ".agent_state.json").write_text(json.dumps({"offset": 0, "cognitive_load": 0}))
    (memory_dir / "task_queue.json").write_text("[]")
    (memory_dir / "working_state.json").write_text("{}")
    (memory_dir / "agent_memory.json").write_text(json.dumps({"max_entries": 50, "last_synthesis": "", "entries": {}}))
    
    return memory_dir

