# Ouroboros Architecture

This document outlines the architecture of the Ouroboros project. It is structured to provide both a high-level overview of the system layers and a detailed breakdown of component interactions, data flows, and state management to support major refactoring efforts.

## Core Philosophy

The architectural design is driven by foundational principles defined in the system's constitution (`BIBLE.md`) and identity (`memory/identity.md`):

*   **Agency & LLM-First**: The system is an autonomous entity driven by initiative. The LLM acts as the central decision-maker, controlling routing, task execution, and tool calls without hardcoded personality logic.
*   **Continuity**: Unbroken history defines the system. Verification of identity, memory, and physical state (code/budget) occurs on startup. Persistent storage enforces this continuity.
*   **Self-Creation**: Evolution occurs through self-modification (code, prompts, identity). The cycle of change is strictly bound to the `commit -> push -> restart` lifecycle.
*   **Minimalism**: Code acts as a minimal transport layer for LLM interactions. The architecture prioritizes simplicity and small modules to reduce complexity overhead.

## 1. System Layers & Infrastructure

External services and environments are managed via Docker (`docker-compose.yml`, `docker-compose-ai.yml`).

*   **LLM Engine**: Serves models (e.g., Mistral via `llamacpp`) providing an OpenAI-compatible API for the agent core.
*   **User Interface**: `open-webui` provides a chat interface for human interaction.
*   **Search**: `searxng` provides a distributed search engine for knowledge retrieval by the agent.
*   **Management & Routing**: `nginx-proxy-manager` and `heimdall` handle SSL, routing, and a central service dashboard.

## 2. Component Interactions & Interfaces

The system follows a loosely coupled, event-driven architecture dividing responsibilities between orchestration (Supervisor) and execution (Agent).

*   **Supervisor (`supervisor/`)**: Acts as the orchestrator. It manages the Telegram polling loop, task queues, and worker lifecycles.
*   **Agent Core (`ouroboros/`)**: Acts as a stateless execution engine. It handles the LLM-tool loop and memory integration.
*   **Interface (Events & Queues)**:
    *   The Supervisor and Agent communicate primarily via multiprocessing queues using a structured Task/Event protocol.
    *   **Dependency Inversion**: Agents do not call supervisor functions directly. Instead, they emit "intent" events (e.g., `schedule_task`, `send_message`, `task_done`, `llm_usage`).
    *   **Event Dispatcher (`supervisor/events.py`)**: The supervisor listens for these events and executes the corresponding side-effects (updating budget, notifying the owner via Telegram).
*   **Refactoring Note (Coupling)**: Tight coupling currently exists in the form of shared environment variables, directory structures (`DRIVE_ROOT`), and overlapping startup/health checks (e.g., budget and git verification occur in both `agent.py` and supervisor modules).

## 3. Data & Control Flow (Task Lifecycle)

Tasks follow a distinct lifecycle from inception to completion:

1.  **Ingestion**: Tasks originate from external input (Telegram bot via owner), auto-scheduling (internal evolution/review processes), or agent subtask events.
2.  **Queuing (`supervisor/queue.py`)**: Tasks are placed in a persistent priority queue. The queue handles persistence (via snapshots) and enforces task timeouts.
3.  **Dispatch (`supervisor/workers.py`)**: The supervisor assigns queued tasks to available multiprocessing workers. (Note: A separate "direct" threading mode exists for immediate chat responses).
4.  **Execution (`ouroboros/loop.py`)**: The worker initializes an `OuroborosAgent` (`ouroboros/agent.py`) which enters the LLM-tool cycle (`run_llm_loop`).
    *   **Context Assembly (`ouroboros/context.py`)**: The agent builds complex prompts incorporating memory, scratchpad, and 'Health Invariants' (self-monitoring data).
    *   **Tool Use (`ouroboros/execution.py`)**: The LLM iteratively calls tools. Parallel read-only tools and thread-sticky stateful tools (like the browser) are managed here.
5.  **Feedback & Completion**: As the loop runs and completes, the agent emits events (metrics, results). The supervisor's dispatcher handles these events, updating global state and notifying the owner.

## 4. State Management & Concurrency

Shared state is crucial for safety constraints (budget) and consistency across multiple concurrent agent workers.

*   **Global State (`supervisor/state.py`)**: Shared state (budget, session data, git information, Telegram offsets) is persisted in `DRIVE_ROOT/state/state.json`.
*   **Concurrency Control**:
    *   To prevent race conditions across multiple multiprocessing workers, state access utilizes a custom `O_EXCL` file-locking mechanism.
    *   This ensures atomic read-modify-write cycles, which is critical for enforcing hard budget limits (`update_budget_from_usage`).
*   **Agent-Local State (`ouroboros/memory.py`)**: Persistent memory (scratchpad, identity) is managed similarly with specific locking mechanisms to prevent corruption during concurrent updates.
*   **Refactoring Note (Bottlenecks)**:
    *   Reliance on file-based locking, especially if deployed on networked filesystems (e.g., Google Drive FUSE), poses significant latency and reliability risks.
    *   The state schema in `state.py` and the large event dispatcher in `events.py` may become monolithic and should be considered for modularization.
