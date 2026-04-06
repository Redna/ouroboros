#!/bin/bash

# Prevent accidental installation on the host machine
if [ ! -f /.dockerenv ]; then
    echo "Warning: Not running in Docker. Skipping hook installation to protect host environment."
    exit 0
fi

HOOK_FILE=".git/hooks/pre-commit"

cat > "$HOOK_FILE" << 'EOF'
#!/bin/bash
export UV_PROJECT_ENVIRONMENT=/tmp/ouroboros-preflight-venv
export UV_CACHE_DIR=/tmp/.uv-cache
export PYTHONDONTWRITEBYTECODE=1
echo "Running pre-flight cognitive checks..."

# 1. Static Type Checking
echo "Executing mypy..."
uv run mypy seed_agent.py || { echo "Type check failed! Commit aborted."; exit 1; }

# 2. Logic Verification
echo "Executing pytest..."
uv run pytest tests/ || { echo "Tests failed! Commit aborted."; exit 1; }

# 3. Constitutional Audit (The Semantic Firewall)
echo "Executing Constitutional Auditor..."
uv run python scripts/constitutional_auditor.py || { echo "Constitution violation detected! Commit aborted."; exit 1; }

echo "All checks passed. Memory committed."
EOF

chmod +x "$HOOK_FILE"
echo "Git pre-commit hook installed successfully."
