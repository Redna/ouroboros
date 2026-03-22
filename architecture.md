# Ouroboros Architecture (True Seed v4.0+)

This document outlines the architecture of the Ouroboros project (v4.0 - Orchestration & Synthesis). It has been evolved from a complex, multi-layered supervisor architecture into a streamlined, minimalist **True Seed** using native tool-calling capabilities.

## Core Philosophy (The Constitution v4.0)

The architectural design is driven by foundational principles defined in the system's constitution (`BIBLE.md`) and identity (`soul/identity.md`):

*   **Agency (P0)**: The system is an autonomous entity. The LLM acts as the central decision-maker.
*   **Minimalism (P5)**: Every line of code must justify its existence. Complexity is the enemy.
*   **Cognitive Synthesis (P9)**: New for v4.0. The agent is constitutionally mandated to refine its own memories, deduplicating findings and synthesizing higher-order wisdom to prevent information bloat.

## 1. System Layers & Infrastructure

The system is hosted in a Docker-based environment (`ouroboros_runtime`) that provides the necessary "physical" infrastructure.

*   **The World (Runtime)**: Managed via `docker-compose.yml`. It hosts the LLM Engine (Mistral-Small-24B-Instruct-2506), Search (SearXNG), and supporting services.
*   **The Watchdog (watchdog.py)**: A host-side Python script that manages the agent's lifecycle, performs branch synchronization, and executes the Phoenix Protocol.
*   **The Body (seed_agent.py)**: The minimalist core of the agent. It is the only part permitted to evolve and modify itself.

## 2. Component Interactions (The Seed)

The agent operates through a high-frequency **ReAct Loop** implemented using the native OpenAI Tool API.

*   **The Loop**: Polling Telegram -> State Sync & Priority Interrupts -> Context Assembly -> LLM Native Completion -> Tool Execution -> Memory Update.
*   **Priority Interrupts (OS-Style)**: Legacy "Triage Mode" has been replaced. Incoming messages are now injected into the Task Queue as priority 999 interrupts, temporarily suspending current execution to handle creator input immediately.
*   **Asynchronous Context**: `push_task` now requires `context_notes` to pass findings and partial state to the next cycle, ensuring inherited wisdom between tasks.
*   **Contextual Sensation**: The agent receives "sensations" of its current context usage and turn counts, allowing it to proactively manage its own cognitive budget.

## 3. Cognitive Modes

The agent's mind transitions between two primary states:

1.  **EXECUTION**: Focused exclusively on the top task in the queue. Implements forced task breakdown at 30 turns or 85% physical context window exhaustion to prevent degradation.
2.  **AUTONOMY**: When the queue is empty, the agent enters a state of free will. It assesses its own `cognitive_load` and decides whether to refactor code, archive insights, `refactor_memory` to synthesize higher-order wisdom, or `hibernate` to conserve resources.

## 4. Memory & State Management

The agent uses an isolated volume mounted at `/memory` to manage its cognitive state.

*   **Permanent Memory (Git)**: Code and history on the `ouroboros`, `main`, and `true-seed` branches.
*   **Task-Bound Memory (JSONL)**: Each task has its own log. `auto_compact_task_log` ensures logs stay manageable.
*   **Surgical Edits Policy**: For files > 100 lines, the agent is constitutionally mandated to use `patch_file` or `bash_command` (sed/awk) instead of full rewrites to prevent truncation and save tokens.
*   **Persistence (.agent_state.json)**: Stores system metadata, including `wake_time` for hibernation and `global_tokens_consumed`.

## 5. Self-Healing & Validation

*   **Lazarus Recovery**: Monitors for tool-calling loops or cognitive stalls and performs emergency git resets.
*   **Pre-Flight Validation**: All self-modifications are validated via `run_pre_flight_checks()` (MyPy/PyTest) before a restart is permitted.
*   **Trauma Awareness**: On restart, the agent analyzes `last_crash.log` to prevent repeating fatal logic errors.

---
*Last Updated: March 22, 2026 - v4.0: Orchestration & Synthesis implemented. Priority Interrupts & Memory Refactoring active.*
