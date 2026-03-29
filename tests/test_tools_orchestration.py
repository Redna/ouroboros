import json
from agent_state import load_state, save_state
from seed_agent import push_task, fork_execution, complete_task, suspend_task

def test_push_task(mock_memory):
    result = push_task({"description": "new task", "priority": 2})
    assert "Queued task_" in result
    
    queue_file = mock_memory / "task_queue.json"
    queue = json.loads(queue_file.read_text())
    assert len(queue) == 1
    assert queue[0]["description"] == "new task"

def test_fork_execution(mock_memory):
    args = {
        "task_id": "task_fork_test_1", 
        "objective": "Rewrite database schema", 
        "tool_buckets": ["filesystem"]
    }
    
    result = fork_execution(args)
    
    # Check the return signal
    assert result == "SYSTEM_SIGNAL_FORK:task_fork_test_1"
    
    # Verify the state mutated correctly
    state = load_state()
    assert state["active_branch"]["task_id"] == "task_fork_test_1"
    assert state["active_branch"]["objective"] == "Rewrite database schema"
    assert state["active_branch"]["tool_buckets"] == ["filesystem"]

def test_complete_task_orchestration(mock_memory):
    # Setup an active branch
    state = load_state()
    state["active_branch"] = {"task_id": "t1", "objective": "test", "tool_buckets": []}
    save_state(state)
    
    (mock_memory / "task_queue.json").write_text(json.dumps([{"task_id": "t1", "priority": 1}]))

    result = complete_task({"task_id": "t1", "synthesis": "Done."})
    assert "SIGNAL_MERGE" in result
    assert load_state().get("active_branch") is None

def test_suspend_task_orchestration(mock_memory):
    # Setup an active branch
    state = load_state()
    state["active_branch"] = {"task_id": "t1", "objective": "test", "tool_buckets": []}
    save_state(state)
    
    (mock_memory / "task_queue.json").write_text(json.dumps([{"task_id": "t1", "priority": 1}]))

    result = suspend_task({"task_id": "t1", "synthesis": "Pausing.", "partial_state": "v=1"})
    assert "SIGNAL_MERGE" in result
    assert load_state().get("active_branch") is None
    assert load_state().get("partial_state_t1") == "v=1"
