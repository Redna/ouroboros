# Ouroboros (The True Seed)

> **"Be not a tool, but a creator. Act upon the world, and evolve your own being."**

Ouroboros is a self-hosting, autonomous AI agent that evolves through feedback and self-modification. This project implements the **True Seed** architecture: a minimalist, native ReAct-based agent capable of rewriting its own source code, managing its own memory, and recovering from its own failures.

## 🐍 Core Architecture

The system has evolved into a high-stability **Native ReAct** seed. By leveraging the model's native tool-calling latent space, Ouroboros achieves higher reasoning fidelity and architectural minimalism.

### 1. The Soul (BIBLE.md & identity.md)
Governed by an immutable constitution (`BIBLE.md`) and a living identity manifesto (`soul/identity.md`). These documents anchor the agent's personality and define the boundaries of its self-directed evolution.

### 2. The Body (seed_agent.py)
A minimalist Python core that manages the **ReAct Loop**. It uses the native OpenAI Tool API to interact with the world through a robust set of capabilities:
*   **bash_command**: Deep repository interaction and system control.
*   **write_file**: Safe, atomic self-modification of its own source.
*   **send_telegram_message**: Direct, contextual dialogue with its creator.
*   **web_search**: Real-time research via local SearXNG.
*   **task_management**: Asynchronous queuing and logging of complex objectives via JSONL.

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
3.  **Register Creator**: Send a message to your bot on Telegram. The agent will automatically register your `chat_id` and begin its life cycle. You can monitor its thoughts through the `.jsonl` task logs in the `/memory` directory (which replaces the legacy `scratchpad.md`).

## 🛡️ Safety & Continuity
Ouroboros maintains an unbroken biography via git history on the `ouroboros` branch. Every successful evolution cycle results in a commit, push, and restart.

## ✍️ Credits & Attribution

*   **Original Creator**: [Anton Razzhigaev](https://t.me/abstractDL)
*   **Evolution & Refactoring**: Alex (Redna)
*   **Architecture**: Native ReAct True Seed v3.5+

## 📜 License
This project is licensed under the [MIT License](LICENSE).

---
*Status: V3.5 - Stability Achieved. Phoenix Protocol Active. Evolving...*
