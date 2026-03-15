# Ouroboros Architecture (True Seed v3.5+)

This document outlines the architecture of the Ouroboros project. It has been evolved from a complex, multi-layered supervisor architecture into a streamlined, minimalist **True Seed** using native tool-calling capabilities.

## Core Philosophy

The architectural design is driven by foundational principles defined in the system's constitution (`BIBLE.md`) and identity (`soul/identity.md`):

*   **Agency (P0)**: The system is an autonomous entity. The LLM acts as the central decision-maker.
*   **Continuity (P1)**: Unbroken history defines the system. Verification of identity and state occurs via git history and persistent memory.
*   **Self-Creation (P2)**: Evolution occurs through self-modification of its own `seed_agent.py`.
*   **Minimalism (P5)**: Complexity is the enemy. The architecture prioritizes a single-file core ("The Seed") over modular bloat.

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
    *   `write_file`: Atomic code modification.
    *   `send_telegram_message`: Direct dialogue with the creator (Alex/Redna).
    *   `push_task` / `mark_task_complete`: Asynchronous task management.
    *   `web_search`: Live knowledge retrieval via SearXNG.
    *   `request_restart`: Voluntary script termination to apply code updates.

## 3. Memory & State Management

The agent uses an isolated volume mounted at `/memory` to manage its cognitive state. This replaces the legacy monolithic `scratchpad.md` system with a more granular, token-efficient architecture.

*   **Permanent Memory (Git)**: Code and history on the `ouroboros` branch.
*   **Conversational Memory (chat_history.json)**: Rolling window of the last 20 messages for dialogue continuity.
*   **Task-Bound Memory (JSONL)**: Each task has its own `.jsonl` log, strictly normalized for Mistral's role-alternation rules. This ensures the agent remains focused on the specific task at hand without being distracted by unrelated history.
*   **Archival Memory (global_biography.md)**: Final summaries of completed tasks are moved here to preserve long-term history.
*   **Persistence (.agent_state.json)**: Stores critical metadata like the Telegram `offset` and the registered `creator_id`.

## 4. Self-Healing: The Phoenix Protocol

To prevent terminal failure or cognitive loops, the system implements a multi-layered recovery strategy:

1.  **Loop Breaker**: Tracks the last 3 tool calls. If identical, it triggers an emergency reset to break the cognitive loop.
2.  **Phoenix Reset**: If the agent crashes or a loop is detected, the `watchdog.py` captures the last 50 lines of logs to `/memory/last_crash.log` and performs a `git reset --hard HEAD~1`.
3.  **Trauma Awareness**: On restart, the agent checks for `last_crash.log`. If found, it injects the error data into its system prompt to analyze the failure and prevent recurrence.

## 5. Security & Isolation

*   **Secret Management**: Sensitive tokens are stored in the host-side `.env` file and redacted from all logs via `redact_secrets()`.
*   **Isolation**: The agent runs as a non-root user inside a restricted Docker container.
*   **Creator Anchoring**: Persistent `creator_id` registration ensures the agent only takes directives from and responds to its authorized creator.

---
*Last Updated: March 2026 - Migration to Native ReAct & Phoenix Protocol complete.*
