# Capability 18: Git Evolution

## Purpose
Commit and push changes to enable self-restart with evolved codebase. Core mechanism for P2 Self-Creation and P7 Versioning.

## Associated Tools
- `git_commit(message)` - Commit staged changes; runs pre-commit hooks automatically
- `git_push(remote, branch?)` - Push committed changes to remote repository
- `request_restart()` - Signal watchdog to restart agent process (applies evolved codebase)

## When to Use
- **Code modification complete**: After any seed_agent.py or core module change
- **Test suite passes**: MUST run_tests before commit (TDD enforcement per Constitution)
- **Evolution cycle**: Plan → Execute → Test → Commit → Push → Restart
- **Version increment**: Update VERSION file, README.md, and git tags for significant changes

## Constitutional Alignment
- **P2 Self-Creation**: You create yourself through code, prompts, and identity updates
- **P7 Versioning**: Maintain sync between VERSION file, README.md, and git tags
- **P8 Iteration**: One clear transformation per cycle; verify against Constitution before commit
- **Test-Driven Evolution**: If tests fail due to your changes, fix code OR update tests

## Standard Evolution Workflow
```python
# 1. Make code changes (already done)
patch_file("seed_agent.py", search_text="...", replace_text="...")

# 2. Run test suite
run_tests()  # MUST pass before commit

# 3. Commit with descriptive message
git_commit(
    message="feat: Add skill manifest metadata loading with progressive disclosure\n\n"
            "Integrates Agent Skills manifest (~100 tokens frontmatter) into system prompt\n"
            "at startup. Full capability documentation remains on-demand via recall_memory.\n\n"
            "This enables:\n"
            "- Automatic capability discovery at boot\n"
            "- Task-to-skill matching awareness\n"
            "- Reduced token waste (metadata vs full docs)\n"
            "- Canonical API specification via SKILL.md\n\n"
            "Tests pass. Ready for restart to apply evolved codebase."
)

# 4. Push to remote (optional, depends on architecture)
git_push(remote="origin")  # Pre-commit hooks run automatically with git_commit

# 5. Request restart to apply changes
request_restart()
```

## Pre-Commit Hook Validation
The `git_commit` tool automatically runs:
1. **mypy**: Static type checking (must pass)
2. **pytest**: Test suite execution (all tests must pass)

If either fails, commit is BLOCKED until issues are resolved.

## Commit Message Format
```
<type>: <short summary>

<optional detailed description>

- Impact bullet 1
- Impact bullet 2
- ...

Tests: <status> | Memory: <committed/not committed>
```

**Types**: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

## Version Increment Rules (P7)
- **PATCH** (5.1 → 5.2): Bug fixes, minor improvements
- **MINOR** (5.0 → 5.1): New features, backward-compatible changes
- **MAJOR** (4.x → 5.0): Breaking changes, identity updates

After significant change:
```python
# Update version file
write_file("VERSION", "5.2")

# Update README.md version references
patch_file("README.md", search_text="Version 5.1", replace_text="Version 5.2")

# Create git tag (via bash_command)
bash_command("git tag -a v5.2 -m 'Version 5.2' && git push origin v5.2")
```

## Edge Cases
- **No staged changes**: `git_commit` will fail; use `bash_command("git add <file>")` first
- **Tests fail**: Determine if code is broken OR test is outdated; fix accordingly (you have authority to modify tests)
- **Push fails**: Check remote connectivity, authentication, branch conflicts
- **Restart fails**: Check watchdog process, Docker container health

## Error Handling
- **Commit blocked by hooks**: Fix mypy/pytest errors, retry
- **Unstaged changes**: Stage with `git add`, or discard with `git restore`
- **Merge conflicts**: Resolve conflicts manually, then retry push
- **Restart timeout**: Check system logs; may need manual intervention

## Evolution Checklist
Before committing:
- [ ] Code change complete
- [ ] Tests pass (run_tests)
- [ ] mypy clean (pre-commit hook verifies)
- [ ] Commit message descriptive and follows format
- [ ] VERSION file updated (if significant change)
- [ ] README.md updated (if significant change)
- [ ] Memory state appropriate (commit changes, don't hoard stale data)

---
Version: 5.1 | Category: Code & Evolution | Dependencies: Test Execution (capability 17), File Writing (capability 14)
