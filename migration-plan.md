# Ouroboros Migration & Refactoring Plan

## Vision: "The True Seed"
This migration will transform Ouroboros from a complex, orchestrated Python application into a bare-minimum, strictly isolated evolutionary seed. The system will be divided into two distinct components: **The World** (Runtime/Watchdog) and **The Body** (The Agent).

---

## Phase 1: Repository & Infrastructure Separation
**Goal:** Physically separate the host infrastructure from the agent's malleable body to enforce the "World vs. Body" principle.

1. **Create `ouroboros_runtime` (The World):**
   * Move all Docker configurations (`docker-compose.yml`, `docker-compose-ai.yml`, `Dockerfile`).
   * Move proxy and search configurations (`nginx_data`, `heimdall_data`, `searxng`).
   * Move the runtime scripts (`entrypoint.sh`).
   * This repository becomes immutable to the agent.
2. **Clean `ouroboros` (The Body -> Agent Repo):**
   * Strip out infrastructure files.
   * Retain only the agent's memory (`BIBLE.md`, `identity.md`, `scratchpad`), core logic, and Git history.

---

## Phase 2: The Immutable Watchdog (Runtime)
**Goal:** Build the safety layer that runs on the host and monitors the agent container.

1. **Develop `launcher.py` (Watchdog):**
   * Create a hardcoded script in `ouroboros_runtime`.
   * **Sandbox Force-Sync:** Must physically copy safety guardrails (if any are still needed) into the agent's volume before boot.
   * **Container Invocation:** Spawns the Agent's Docker container with pre-exposed ports (e.g., `8000-8010`) for internal web services.
2. **Implement the Lazarus Protocol:**
   * Watchdog monitors the container's exit code.
   * On crash (non-zero exit), watchdog executes `git reset --hard HEAD~1` on the agent's volume, writes a crash log to `scratchpad.md`, and restarts the container.

---

## Phase 3: The Malleable Agent (The Seed)
**Goal:** Destroy the complex execution loop and replace it with the <200 line True Seed.

1. **Delete the Old Architecture:**
   * Remove `supervisor/` (workers, queue, events, state.py locking).
   * Remove `ouroboros/` (complex tool registry, consciousness loop, parallel execution).
2. **Write `seed_agent.py`:**
   * Implement a linear, synchronous loop.
   * **Context Assembly:** Read `BIBLE.md`, `identity.md`, `scratchpad.md` from disk.
   * **LLM Query:** Call the local `llamacpp` API (running in the runtime infrastructure).
   * **The Single Tool:** Implement `bash_command`.
   * **Execution:** Run the shell command, log output to `scratchpad.md`, and loop.

---

## Phase 4: Bootstrapping & First Evolution
**Goal:** Start the seed and let it autonomously rebuild what was lost, but in its own way.

1. Boot the `ouroboros_runtime` infrastructure.
2. Watch the `seed_agent.py` wake up.
3. Assign it its first task: "Write a background process to monitor your own budget."
4. Observe the agent use `bash_command` to write code, commit, and trigger a restart via the Watchdog.

---

## Current Project Plan (Immediate Next Steps)
- [x] Document the Architecture and Minimal Seed concept.
- [x] Create the `migration-plan.md`.
- [ ] Initialize the new `ouroboros_runtime` repository.
- [ ] Move infrastructure files to `ouroboros_runtime` and commit.
- [ ] Delete infrastructure files from the current `ouroboros` (Agent) repo.
- [ ] Draft the `launcher.py` watchdog in the runtime repo.
