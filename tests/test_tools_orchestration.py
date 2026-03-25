import json
from seed_agent import push_task, fork_execution, merge_and_return, load_state, save_state

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

def test_merge_and_return(mock_memory):
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

    result = merge_and_return(args)
    
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
