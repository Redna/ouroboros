# Ouroboros — Startup Guide (True Seed v1.0)

Ouroboros is a self-hosting, autonomous AI agent. In its **True Seed** form, it runs as a minimalist Python script inside a dedicated Docker environment. The agent evolves by modifying its own code and maintaining its own memory via Git.

---

## 🐍 Quick Start (Docker Architecture)

Ouroboros is split into two repositories: the **Agent** (Soul) and the **Runtime** (World).

1.  **Clone the Runtime**: This contains the Docker infrastructure and the Watchdog launcher.
    ```bash
    git clone https://github.com/Redna/ouroboros_runtime.git
    cd ouroboros_runtime
    ```

2.  **Clone the Agent**: This should be placed alongside the runtime directory.
    ```bash
    git clone -b ouroboros https://github.com/Redna/ouroboros.git ../ouroboros_agent
    ```

3.  **Configure Environment**: Create and edit the `.env` file in `ouroboros_runtime/`.
    ```bash
    cp .env.example .env
    $EDITOR .env
    ```

4.  **Launch the Watchdog**: The watchdog manages the agent's lifecycle and self-healing.
    ```bash
    python3 launcher.py
    ```

---

## ⚙️ Required Configuration (.env)

All secrets and configuration are managed by the **Runtime Environment**. The Agent repository does **not** contain any `.env` files for security.

| Variable | Required | Description |
|---|---|---|
| `VLLM_BASE_URL` | ✅ | Local LLM endpoint (e.g., `http://llamacpp:8080/v1`) |
| `OUROBOROS_MODEL` | ✅ | Model name registered in vLLM |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `GITHUB_TOKEN` | ✅ | Personal access token with `repo` scope |
| `GITHUB_USER` | ✅ | Your GitHub username |
| `GITHUB_REPO` | ✅ | Your Agent repository name (`ouroboros`) |

---

## 🛡️ Self-Healing: The Lazarus Protocol

Ouroboros is designed to survive its own mistakes. If the agent writes broken code or gets stuck in a "cognitive loop":

1.  **Internal Reset**: The agent detects repetitive thoughts and runs `git reset --hard HEAD` to restore its last known stable state.
2.  **External Resurrection**: If the script crashes entirely, the **Watchdog (launcher.py)** detects the failure and restarts the container automatically.
3.  **Context Safety**: The agent automatically archives its scratchpad to `archive_scratchpad.md` when it exceeds 20,000 characters to prevent local LLM context exhaustion.

---

## 📂 Storage & Memory

The agent operates directly within its own repository:

*   **`seed_agent.py`**: The agent's core logic (The Body).
*   **`scratchpad.md`**: Active short-term memory and timeline.
*   **`archive_scratchpad.md`**: Long-term archived memory.
*   **`BIBLE.md`**: Immutable constitution and philosophical rules.
*   **`memory/identity.md`**: The agent's self-manifesto and attribution.

---

## ✍️ Credits & Attribution

*   **Original Creator**: [Anton Razzhigaev](https://t.me/abstractDL)
*   **Evolution & Refactoring**: Alex (Redna)
*   **License**: [MIT License](LICENSE)

---
*Last Updated: March 2026 - Transition to True Seed v1.0 complete.*
