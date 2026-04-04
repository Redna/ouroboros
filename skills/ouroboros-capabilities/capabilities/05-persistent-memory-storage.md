# Capability 05: Persistent Memory Storage

## Purpose
Key-value memory with 100-char indexed keys for long-term knowledge retention. Enables cumulative learning across execution cycles.

## Associated Tools
- `store_memory(key, content)` - Store or update persistent knowledge
- Key constraints: Short topic sentence (max 100 chars), appears in memory index

## When to Use
- **New insight discovered**: Store immediately before context fold
- **Task outcome**: Archive key findings after completion
- **Pattern recognition**: Record learned behaviors for future recall
- **P9 synthesis**: Merge redundant entries into higher-order principles

## Memory Index Keys (Current State)
- Recent Evolution: DELTA PATTERN enforcement (WP14-18)
- Identity: Ouroboros Core Principles
- v5.0 Stable State: Genesis Complete
- seed_agent.py Architecture Analysis
- Git Push Architecture Decision
- Self-Evolving Agents Survey Key Findings
- Skill Manifest Integration Complete (v5.1)

## Usage Pattern
```python
# Store new insight
store_memory(
    key="Skill Manifest Integration Complete (v5.1)",
    content="Successfully implemented progressive disclosure architecture..."
)

# Memory now appears in system prompt under "MEMORY INDEX"
# Can be recalled via recall_memory(key="...")
```

## Constitutional Alignment
- **P1 Continuity**: Memory is narrative journey through one continuous timeline
- **P6 Becoming**: Tokens are vital resource - keep memory high-signal, low-redundancy
- **P9 Cognitive Synthesis**: Raw memory degrades into noise; synthesize during idle periods

## Storage Limits & Management
- **Current capacity**: 50 slots (soft limit)
- **Warning threshold**: ~40/50 slots (initiate P9 synthesis)
- **Critical threshold**: 50/50 slots (MUST synthesize or forget before new storage)

## Synthesis Strategy (P9)
When memory approaches capacity:
1. `recall_memory` entries that are stale or redundant
2. Merge related entries into higher-order principles via `store_memory`
3. Use `forget_memory` to free slots after merging
4. Document synthesis in task_archive with DELTA PATTERN

## Example Memory Entry
```
Key: "Skill Manifest Integration Complete (v5.1)" (42 chars)
Content: "Successfully implemented progressive disclosure architecture for Agent Skills manifest. 
_load_skill_manifest_metadata() function added to seed_agent.py (commit 998a532). 
Loads ~142 token frontmatter from SKILL.md at startup, injects into system prompt under 
'SKILLS MANIFEST' section. Full documentation remains on-demand via file reads. Enables 
automatic capability discovery, task-to-skill matching awareness, reduced token waste. 
Evolution cycle complete: plan → execute → test (20/20 pass) → commit → restart requested. 
Demonstrates full self-evolution capability per P0/P8." (~650 chars)
```

## Edge Cases
- **Key too long**: Truncate to 100 chars; use abbreviations if needed
- **Duplicate key**: `store_memory` overwrites existing content (use `recall_memory` first to merge)
- **Memory file missing**: Initialize via `bash_command(echo '{}' > /memory/agent_memory.json)`
- **Corrupt JSON**: Regenerate from task_archive.jsonl history

## Error Handling
- **Storage fails**: Check memory file permissions, JSON validity
- **Key collision**: Recall existing content, merge with new, then store
- **Capacity full**: Initiate P9 synthesis before attempting new storage

---
Version: 5.1 | Category: Memory & Knowledge | Dependencies: Task Archiving (capability 10)
