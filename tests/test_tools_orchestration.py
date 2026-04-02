import json
from agent_state import load_state, save_state
from seed_agent import push_task, complete_task, suspend_task

def test_push_task(mock_memory):
    result = push_task({"description": "new task", "priority": 2})
    assert "Queued task_" in result
    
    queue_file = mock_memory / "task_queue.json"
    queue = json.loads(queue_file.read_text())
    assert len(queue) == 1
    assert queue[0]["description"] == "new task"

def test_complete_task_orchestration(mock_memory):
    (mock_memory / "task_queue.json").write_text(json.dumps([{"task_id": "t1", "priority": 1}]))

    result = complete_task({"task_id": "t1", "synthesis": "Done."})
    assert "completed and removed from queue" in result

def test_suspend_task_orchestration(mock_memory):
    (mock_memory / "task_queue.json").write_text(json.dumps([{"task_id": "t1", "priority": 1}]))

    result = suspend_task({"task_id": "t1", "synthesis": "Pausing.", "partial_state": "v=1"})
    assert "suspended" in result
    assert load_state().get("partial_state_t1") == "v=1"