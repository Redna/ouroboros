# Ouroboros Project Protocol

- **Branching Policy:**
  - Always perform changes on the `true-seed` branch.
  - If necessary, rebase the `ouroboros` branch on `true-seed`.
  - NEVER merge into `main`.
  - DO NOT push to `main`.
- **Pre-Change Requirements:**
  - Ensure the Ouroboros agent is stopped before any code modifications.
  - Check the status using `/home/alexander/Ouroboros_Project/ouroboros_runtime/ouroboros status`.
