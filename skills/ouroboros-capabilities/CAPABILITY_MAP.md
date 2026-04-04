# Ouroboros Capability Map

This file documents the modular capability architecture for progressive disclosure loading.

## Directory Structure

```
/app/skills/ouroboros-capabilities/
├── CAPABILITY_MAP.md          # This file - capability index
├── capabilities/              # Individual capability modules (21 total)
│   ├── 01-task-queue-orchestration.md
│   ├── 02-cognitive-parameter-control.md
│   ├── 03-context-folding.md
│   ├── 04-introspective-reflection.md
│   ├── 05-persistent-memory-storage.md
│   ├── 06-memory-recall.md
│   ├── 07-memory-forgetting.md
│   ├── 08-memory-archive-search.md
│   ├── 09-memory-rewriting.md
│   ├── 10-task-archiving.md
│   ├── 11-web-search.md
│   ├── 12-webpage-fetching.md
│   ├── 13-repository-mapping.md
│   ├── 14-file-writing.md
│   ├── 15-surgical-patching.md
│   ├── 16-file-reading.md
│   ├── 17-test-execution.md
│   ├── 18-git-evolution.md
│   ├── 19-telegram-messaging.md
│   ├── 20-task-queue-management.md
│   └── 21-system-hibernation.md
├── references/               # Detailed reference documentation
│   └── CAPABILITIES.md       # Full detailed specs (existing)
└── SKILL.md                  # Root manifest (existing - metadata + overview)
```

## Capability Categories

### Cognitive & State Management (4 capabilities)
| # | Capability | Tool(s) | Purpose |
|---|------------|---------|---------|
| 01 | Task Queue Orchestration | `push_task`, `complete_task`, `dismiss_queue_item` | Prioritized task management with deferral support |
| 02 | Cognitive Parameter Control | `set_cognitive_parameters` | Dynamic temperature and thinking mode adjustment |
| 03 | Context Folding | `fold_context` | DELTA PATTERN synthesis for context reset without amnesia |
| 04 | Introspective Reflection | `reflect` | Internal monologue for debugging and planning |

### Memory & Knowledge (6 capabilities)
| # | Capability | Tool(s) | Purpose |
|---|------------|---------|---------|
| 05 | Persistent Memory Storage | `store_memory` | Key-value memory with 100-char indexed keys |
| 06 | Memory Recall | `recall_memory` | Retrieve detailed content by key or substring search |
| 07 | Memory Forgetting | `forget_memory` | Free slots by removing obsolete entries |
| 08 | Memory Archive Search | `search_memory_archive` | Full-text search across /memory volume |
| 09 | Memory Rewriting | `rewrite_memory` | Overwrite or synthesize memory files |
| 10 | Task Archiving | N/A (via `complete_task`) | Append-only completed task records with DELTA PATTERN |

### Research & Information (3 capabilities)
| # | Capability | Tool(s) | Purpose |
|---|------------|---------|---------|
| 11 | Web Search | `web_search` | Local SearXNG search for external information |
| 12 | Webpage Fetching | `fetch_webpage` | Download URLs to Markdown format |
| 13 | Repository Mapping | `generate_repo_map` | AST-based codebase structure analysis |

### Code & Evolution (5 capabilities)
| # | Capability | Tool(s) | Purpose |
|---|------------|---------|---------|
| 14 | File Writing | `write_file` | Overwrite files with new content |
| 15 | Surgical Patching | `patch_file` | Replace specific text blocks without full file rewrite |
| 16 | File Reading | `read_file_tool` | Line-range or full file content retrieval |
| 17 | Test Execution | `run_tests` | Run test suite before commits (TDD enforcement) |
| 18 | Git Evolution | `git_commit`, `git_push`, `request_restart` | Commit and push changes to enable self-restart |

### Communication & Coordination (3 capabilities)
| # | Capability | Tool(s) | Purpose |
|---|------------|---------|---------|
| 19 | Telegram Messaging | `send_telegram_message` | Direct communication with creator (P4 authenticity) |
| 20 | Task Queue Management | `push_task`, `complete_task`, `dismiss_queue_item` | Push, complete, dismiss tasks with synthesis |
| 21 | System Hibernation | `reflect(status="standby")` | Resource-efficient sleep cycles (30s-120s) |

## Loading Strategy

### Metadata-Only Startup (~142 tokens)
```python
def _load_skill_manifest_metadata() -> str:
    # Loads SKILL.md frontmatter only
    # Returns: "name: ouroboros-core-agent | description: ... | license: MIT | ..."
```

### On-Demand Capability Loading (200-400 tokens per capability)
```python
def load_capability(capability_name: str) -> str:
    """Load single capability module on-demand."""
    path = Path("/app/skills/ouroboros-capabilities/capabilities/") / f"{capability_name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else f"Capability {capability_name} not found."
```

### Context-Triggered Discovery
When a task arrives:
1. Analyze task description for capability keywords
2. Load only relevant capability modules
3. Execute task with targeted knowledge
4. Unload after completion (token efficiency)

## Token Efficiency

| Loading Strategy | Tokens | Use Case |
|-----------------|--------|----------|
| Metadata only | ~142 | Startup, all tasks |
| Single capability | ~200-400 | Focused task execution |
| Category (5-6 caps) | ~1,000-2,000 | Complex multi-tool tasks |
| All capabilities | ~5,000 | Full context needed (rare) |

**Target:** 97% token savings vs loading all docs at startup.

## Constitutional Alignment

All capabilities operate under P0-P9 priority hierarchy. Each capability module documents:
- Which principles it primarily serves
- Error handling aligned with Constitution
- Edge cases and failure modes

## Version Control

All capability files in `/app/skills/` are git-tracked. Evolution follows:
1. Modify capability file(s)
2. Test changes (run_tests)
3. Commit with clear message
4. Push to remote
5. Request restart to apply changes

---
Generated: 2025-04-04 | Version: 5.1 | Total Capabilities: 21
