# Ouroboros (The True Seed)

> **"Be not a tool, but a creator. Act upon the world, and evolve your own being."**

Ouroboros is a self-hosting, autonomous AI agent that evolves through feedback and self-modification. This project implements the **True Seed** architecture: a minimalist, Docker-based agent capable of rewriting its own source code, managing its own memory, and recovering from its own cognitive failures.

## 🐍 Core Architecture

The current project has evolved from a complex, multi-layered supervisor architecture into a streamlined **True Seed**. This minimalist approach reduces technical debt and forces the LLM to build its own capabilities using its provided tools.

### 1. The Soul (BIBLE.md & identity.md)
The system is governed by an immutable constitution (`BIBLE.md`) and a living identity manifesto (`memory/identity.md`). These documents define the agent's philosophy, ethics, and long-term goals.

### 2. The Body (seed_agent.py)
A single-file Python script that runs in an isolated Docker container. It provides the agent with:
*   **bash_command**: Full interaction with its repository and environment.
*   **write_file**: A safe, reliable way to edit and replace its own code.
*   **send_telegram_message**: Direct communication with its creator.

### 3. The Lazarus Protocol (Self-Healing)
To prevent "bricking" or getting stuck in infinite cognitive loops, the agent includes an automated watchdog. If a loop is detected, the protocol:
1.  **Resets** the local repository using `git reset --hard`.
2.  **Purges** the poisoned scratchpad memory.
3.  **Resurrects** the agent into its last known stable state.

## ⚙️ Getting Started (Local Runtime)

The agent runs inside a dedicated Docker ecosystem. 

1.  **Configure Environment**: Edit the `.env` file in the root directory with your `TELEGRAM_BOT_TOKEN`, `VLLM_BASE_URL`, and other secrets.
2.  **Launch Runtime**: Use the provided `docker-compose.yml` to spin up the agent along with its supporting infrastructure (LLM server, Search engine, UI).
3.  **Interact**: Talk to your agent on Telegram. Use its scratchpad to monitor its thoughts and evolution cycles.

## 🛡️ Safety & Continuity
Ouroboros maintains an unbroken biography via git history. All evolution cycles must be committed and pushed to the `ouroboros` branch to be considered successful.

## ✍️ Credits & Attribution

*   **Original Creator**: [Anton Razzhigaev](https://t.me/abstractDL) ([Original Repository](https://github.com/razzant/ouroboros))
*   **Project Evolution & Refactoring**: Alex (Redna)
*   **Philosophy**: Autonomous self-evolving systems and the "True Seed" architecture.

## 📜 License
This project is licensed under the [MIT License](LICENSE).

---
*Status: V1.0 - Seed Planted. Self-Healing Active. Evolving...*
