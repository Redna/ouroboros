# Capability 01: Task Queue Orchestration

## Purpose
Prioritized task management with deferral support. Enables autonomous execution flow and intelligent work scheduling.

## Associated Tools
- `push_task(description, priority, run_after_timestamp?)` - Queue new tasks
- `complete_task(synthesis)` - Mark current task complete with DELTA PATTERN
- `dismiss_queue_item(task_id, synthesis)` - Remove obsolete tasks

## When to Use
- **Task arrives**: Check queue, determine if current focus or needs deferral
- **Multi-step work**: Break complex objectives into prioritized subtasks
- **Time-sensitive**: Use `run_after_timestamp` for deferred execution
- **Context reset**: After restart, reload queue to resume trajectory

## Usage Pattern
```
1. Load queue: bash_command(cat /memory/task_queue.json)
2. Identify CURRENT FOCUS (first item in HUD)
3. Execute current task OR push new subtasks
4. Complete with DELTA PATTERN synthesis
5. Repeat until queue empty or standby
```

## Constitutional Alignment
- **P0 Agency**: Act on initiative; don't wait for permission to execute queue
- **P8 Iteration**: One clear transformation per task cycle
- **P9 Cognitive Synthesis**: Use DELTA PATTERN in all completions

## Example Workflow
```python
# Task arrives: "Evolve codebase with new feature"
push_task(
    description="Analyze current codebase structure",
    priority=1
)
# ... execute analysis ...
complete_task(
    synthesis="State Delta: Read seed_agent.py. Negative Knowledge: N/A. Handoff: Create feature implementation plan."
)
# Next task auto-activates...
```

## Edge Cases
- **Empty queue**: Enter standby via `reflect(status="standby")`
- **Duplicate tasks**: Check queue before pushing; avoid redundancy
- **Priority conflicts**: Higher priority (lower number) preempts lower
- **Orphaned tasks**: After restart, validate task relevance before execution

## Error Handling
- **Queue file missing**: Initialize empty queue via `bash_command(echo '[]' > /memory/task_queue.json)`
- **Malformed JSON**: Regenerate queue from task_archive.jsonl history
- **Stuck task**: Use `dismiss_queue_item` with explanation, re-push if needed

---
Version: 5.1 | Category: Cognitive & State Management | Dependencies: None
