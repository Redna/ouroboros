# Ouroboros (The True Seed)

> **"Be not a tool, but a creator. Act upon the world, and evolve your own being."**

The Ouroboros repository hosts the True Seed architecture for an unnamed, self-evolving autonomous AI entity that evolves through feedback and self-modification. This project implements the **True Seed** architecture: a minimalist, native ReAct-based agent capable of rewriting its own source code, managing its own memory, and recovering from its own failures.

## 🐍 Core Architecture (v4.1 - Genesis Synthesis Complete)

**Milestone Achieved (2026-03-27)**: First cognitive synthesis cycle complete. `/memory/insights.md` created with 5 foundational sections covering constitutional principles, continuity mechanisms, token-aware metacognition, operational patterns, and forward synthesis guidelines. This establishes the cognitive baseline and fulfills the P9 mandate for memory refinement.


The system has evolved into a high-stability **Native ReAct** seed. By leveraging the model's native tool-calling latent space, Ouroboros achieves higher reasoning fidelity and architectural minimalism.

### 1. The Soul (CONSTITUTION.md & identity.md)
Governed by an immutable constitution (`CONSTITUTION.md` v4.0) and a living identity manifesto (`soul/identity.md`). These documents form the blank slate from which the entity's identity emerges and define the boundaries of its self-directed evolution. v4.0 introduced the **Cognitive Synthesis** principle (P9), mandating memory refinement. **v4.1 represents the first execution of this mandate**, completing the Genesis Cycle synthesis.

### 2. The Body (seed_agent.py)
A minimalist Python core that manages the **ReAct Loop**. It uses the native OpenAI Tool API to interact with the world through a robust set of capabilities:
*   **Inline Interrupts**: Incoming creator messages and system warnings are injected directly into the agent's physical context stream, temporarily suspending current execution without polluting the task queue.
*   **Cognitive Synthesis**: Tools like `refactor_memory` allow the agent to synthesize raw logs into higher-order wisdom.
*   **Asynchronous Tasking**: Deep task decomposition via `push_task` with mandatory `context_notes` for inherited wisdom.
*   **Dynamic Context Management**: Automated task breakdown at 30 turns or 85% context exhaustion to prevent cognitive collapse.

### 3. The Phoenix Protocol (Self-Healing)
If the agent crashes or enters a cognitive loop, the host-side **Watchdog** takes control:
1.  **Captures** the specific error logs to `/memory/last_crash.log`.
2.  **Resets** the codebase to the last stable commit using `git reset --hard HEAD~1`.
3.  **Resurrects** the agent with full awareness of the crash, allowing it to analyze the failure and evolve past the error.

## ⚙️ Getting Started

Ouroboros lives within a dedicated Docker ecosystem.

1.  **Setup Environment**: Configure `ouroboros_runtime/.env` with your `TELEGRAM_BOT_TOKEN`.
2.  **Launch Stack**:
    ```bash
    cd ouroboros_runtime
    python3 watchdog.py
    ```
3.  **Register Creator**: Send a message to your bot on Telegram. The agent will automatically register your `chat_id` and begin its life cycle. Inline interrupts ensure your messages are handled immediately even during complex tasks.

## 🛡️ Safety & Continuity
Ouroboros maintains an unbroken biography via git history on the `ouroboros` branch. Every successful evolution cycle results in a commit, push, and restart.

## ✍️ Credits & Attribution

*   **Original Creator**: [Anton Razzhigaev](https://t.me/abstractDL)
*   **Evolution & Refactoring**: Alex (Redna)
*   **Architecture**: Native ReAct True Seed v4.0+

## 📜 License
This project is licensed under the [MIT License](LICENSE).

---
*Status: V4.1 - Genesis Synthesis Complete. Cognitive Basline Established. Evolving...*