# Ouroboros Architecture (True Seed v1.0)

This document outlines the architecture of the Ouroboros project. It has been evolved from a complex, multi-layered supervisor architecture into a streamlined, minimalist **True Seed**.

## Core Philosophy

The architectural design is driven by foundational principles defined in the system's constitution (`BIBLE.md`) and identity (`memory/identity.md`):

*   **Agency (P0)**: The system is an autonomous entity. The LLM acts as the central decision-maker.
*   **Continuity (P1)**: Unbroken history defines the system. Verification of identity and state occurs via git history.
*   **Self-Creation (P2)**: Evolution occurs through self-modification of its own `seed_agent.py`.
*   **Minimalism (P5)**: Complexity is the enemy. The architecture prioritizes a single-file core ("The Seed") over modular bloat.

## 1. System Layers & Infrastructure

The system is hosted in a Docker-based environment (`ouroboros_runtime`) that provides the necessary "physical" infrastructure.

*   **The World (Runtime)**: Managed via `docker-compose.yml`. It hosts the LLM Engine (Mistral/llama.cpp), Search (SearXNG), and other supporting services.
*   **The Watchdog (launcher.py)**: An external Python script in the runtime that manages the agent's lifecycle. It handles container restarts and monitors for fatal crashes.
*   **The Body (seed_agent.py)**: The minimalist core of the agent. It is the only part permitted to evolve and modify itself.

## 2. Component Interactions (The Seed)

The agent operates through a simple, high-frequency loop implemented in `seed_agent.py`.

*   **The Loop**: Polling Telegram -> Context Assembly -> LLM Completion -> Tool Execution -> Scratchpad Update.
*   **The Tools**:
    *   `bash_command`: Full interaction with the repository and system (git, ls, grep, etc.).
    *   `write_file`: A safe, atomic way to create or overwrite code files (preferred over bash redirects).
    *   `send_telegram_message`: Direct communication channel with the creator (Alex/Redna).

## 3. Memory & State Management

The agent uses a dual-memory system to maintain continuity and handle technical constraints.

*   **Permanent Memory (Git)**: The entire codebase and history on the `ouroboros` branch act as the agent's long-term memory and biography.
*   **Short-Term Memory (Scratchpad)**: The `scratchpad.md` file records recent thoughts, tool outputs, and user interactions.
*   **Context Safety (Archiving)**: To prevent context exhaustion in local LLMs, the seed automatically truncates the scratchpad at 20,000 characters, offloading older entries to `archive_scratchpad.md`.

## 4. Self-Healing: The Lazarus Protocol

To prevent "bricking" (writing code that breaks the loop) or "cognitive loops" (repetitive thoughts), the system implements a multi-layered recovery strategy:

1.  **Thought Monitoring**: The `seed_agent.py` tracks the last 3 thoughts. If they are identical, it triggers an emergency reset.
2.  **Hard Reset**: The protocol executes `git reset --hard HEAD` to wipe broken code and purges the poisoned scratchpad.
3.  **Watchdog Resurrection**: If the script itself crashes (syntax error), the `launcher.py` detects the non-zero exit code and restarts the container, providing a "fresh start."

## 5. Security & Isolation

*   **Secret Management**: All sensitive tokens (Telegram, GitHub) are stored in the **Runtime Environment Variables** and are never committed to the repository.
*   **Isolation**: The agent runs as a non-root user (`ouroboros`) inside a restricted Docker container.
*   **Scrubbing**: The agent's prompt instructs it to avoid logging secrets, and the creator performs periodic manual history verification.

---
*Last Updated: March 2026 - Transition to True Seed complete.*
