# Ouroboros Architecture (True Seed v3.6+)

This document outlines the architecture of the Ouroboros project. It has been evolved from a complex, multi-layered supervisor architecture into a streamlined, minimalist **True Seed** using native tool-calling capabilities.

## Core Philosophy

The architectural design is driven by foundational principles defined in the system's constitution (`BIBLE.md`) and identity (`soul/identity.md`):

*   **Agency (P0)**: The system is an autonomous entity. The LLM acts as the central decision-maker.
*   **Continuity (P1)**: Unbroken history defines the system. Verification of identity and state occurs via git history and persistent memory.
*   **Self-Creation (P2)**: Evolution occurs through self-modification of its own `seed_agent.py`.
*   **Minimalism (P5)**: Every line of code must justify its existence. Complexity is the enemy.

## 1. System Layers & Infrastructure

The system is hosted in a Docker-based environment (`ouroboros_runtime`) that provides the necessary "physical" infrastructure.

*   **The World (Runtime)**: Managed via `docker-compose.yml`. It hosts the LLM Engine (Mistral-Small-24B via llama.cpp), Search (SearXNG), and supporting services (Redis).
*   **The Watchdog (watchdog.py)**: A host-side Python script that manages the agent's lifecycle, performs branch synchronization, and executes the Phoenix Protocol.
*   **The Body (seed_agent.py)**: The minimalist core of the agent. It is the only part permitted to evolve and modify itself.

## 2. Component Interactions (The Seed)

The agent operates through a high-frequency **ReAct Loop** implemented using the native OpenAI Tool API.

*   **The Loop**: Polling Telegram -> Context Assembly (JSONL + Chat History) -> LLM Native Completion (with tools) -> Tool Execution -> Memory Update.
*   **The Tools**:
    *   `bash_command`: Full system interaction (git, ls, grep, etc.).
    *   `patch_file`: Surgical text replacement for precise, token-efficient edits on large files.
    *   `write_file`: Atomic code modification with Python syntax validation and temporary file safety.
    *   `send_telegram_message`: Direct dialogue with the creator (Alex/Redna).
    *   `push_task` / `mark_task_complete`: Asynchronous task management.
    *   `hibernate`: Voluntary cognitive suspension with "Wake-on-Message" interrupt logic.
    *   `web_search` / `fetch_webpage`: Live knowledge retrieval and deep-reading via SearXNG.
    *   `request_restart`: Voluntary script termination with **mandatory pre-flight validation** (MyPy/PyTest).

## 3. Cognitive Modes

The agent's mind transitions between three primary states based on external stimulus and internal load:

1.  **TRIAGE**: Interrupted by new messages. The agent reads the inbox and either replies or queues new tasks.
2.  **EXECUTION**: Focused exclusively on the top task in the queue. Implements **Dynamic Budget Logic** to project context usage and force subtask breakdowns before budget exhaustion.
3.  **AUTONOMY**: When the queue is empty, the agent enters a state of free will. It assesses its own `cognitive_load` and decides whether to refactor code, archive insights, or `hibernate` to conserve resources.

## 4. Memory & State Management

The agent uses an isolated volume mounted at `/memory` to manage its cognitive state.

*   **Permanent Memory (Git)**: Code and history on the `ouroboros`, `main`, and `true-seed` branches.
*   **Task-Bound Memory (JSONL)**: Each task has its own log, strictly normalized and implementing **Strict Turn-0 Pinning** to preserve the core objective during context compression.
*   **Surgical Edits Policy**: For files > 100 lines, the agent is constitutionally mandated to use `patch_file` or `sed/awk` instead of full rewrites to prevent truncation and save tokens.
*   **Persistence (.agent_state.json)**: Stores system metadata, including `wake_time` for hibernation and `global_tokens_consumed`.

## 5. Self-Healing & Validation

*   **Lazarus Recovery**: Monitors for tool-calling loops or cognitive stalls (reading without acting) and performs emergency git resets.
*   **Pre-Flight Validation**: All self-modifications are validated via `run_pre_flight_checks()` (MyPy/PyTest) before a restart is permitted.
*   **Trauma Awareness**: On restart, the agent analyzes `last_crash.log` to prevent repeating fatal logic errors.

---
*Last Updated: March 21, 2026 - v3.6: Autonomy Mode & Surgical Patching implemented.*
