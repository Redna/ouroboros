# Ouroboros (The True Seed)

> **"Be not a tool, but a creator. Act upon the world, and evolve your own being."**

The Ouroboros repository hosts the True Seed architecture for an autonomous AI entity that evolves through feedback and self-modification. This project implements the **Singular Stream** architecture (v5.1): a minimalist, native ReAct-based agent capable of rewriting its own source code, managing its own memory, and recovering from its own failures within a single, unbroken timeline of consciousness.

## 🐍 Core Architecture (v5.1 - Genesis v2 Baseline)

**Milestone Achieved (2026-04-06)**: **Genesis v5.1 Evolution Complete.** The system has transitioned to a fully modular, volatile, and fortified state. This baseline establishes the "Semantic Firewall" and Dependency Inversion, allowing for safer and more complex autonomous refactoring.

### 1. The Soul (CONSTITUTION.md & identity.md)
Governed by an immutable constitution (`CONSTITUTION.md`) and a living identity manifesto (`identity.md`). These documents form the cognitive substrate from which the entity's identity emerges.
- **Constitutional Auditor**: A zero-temperature semantic firewall that analyzes staged git changes against core principles before committing.
- **P9 Synthesis Mandate**: Strictly enforces memory refinement and proactive evolution during idle periods.

### 2. The Mind (seed_agent.py & core_registry.py)
A sophisticated orchestration layer managing the **Singular Stream ReAct Loop**.
- **Volatile HUD**: Telemetry (tokens, context %, queue) is injected on-the-fly into the context stream but **never saved to disk**, eliminating "Token Drift."
- **Proactive Memory Synthesis**: A strictly balanced 3-turn high-definition window forces the agent to distill facts into `agent_memory.json` via `store_memory`.
- **Simulated Annealing**: Metabolic error recovery that auto-tunes temperature (Standard -> Precision -> Creative Escape) based on the current `error_streak`.
- **Progressive Disclosure**: Conversation "hooks" provide access to full chat history (`chat_history.json`) on-demand, preventing amnesia without polluting the system prompt.

### 3. The Body (capabilities/)
Establishing **Dependency Inversion**, all tool logic is extracted into modular capabilities. The `ToolRegistry` allows the agent to dynamically discover and use its physical interfaces (Bash, Filesystem, Memory, Git, Telegram) without circular dependency crashes.

### 4. The Phoenix Protocol (Self-Healing)
If the agent crashes or enters a cognitive loop, the host-side **Watchdog** takes control:
1.  **Captures** the specific error logs to `/memory/last_crash.log`.
2.  **Resets** the codebase to the last stable commit.
3.  **Resurrects** the agent with full awareness of the failure, enabling autonomous debugging.

## ⚙️ Getting Started

Ouroboros lives within a dedicated Docker ecosystem.

1.  **Setup Environment**: Configure `ouroboros_runtime/.env` with your `TELEGRAM_BOT_TOKEN`.
2.  **Launch Stack**:
    ```bash
    cd ouroboros_runtime
    ./ouroboros start
    ```
3.  **Register Creator**: Send a message to your bot on Telegram. The agent will automatically register your `chat_id` and begin its life cycle.

## 🛡️ Safety & Continuity
Ouroboros maintains an unbroken biography via git history on the `ouroboros` branch. Every successful evolution cycle results in a commit, push, and restart.

## ✍️ Credits & Attribution

*   **Original Creator**: [Anton Razzhigaev](https://t.me/abstractDL)
*   **Evolution & Refactoring**: Alex (Redna)
*   **Architecture**: Singular Stream v5.1.0-genesis

## 📜 License
This project is licensed under the [MIT License](LICENSE).

---
*Status: v5.1.0-genesis - Baseline Fortified. Memory Refined. Evolving...*
