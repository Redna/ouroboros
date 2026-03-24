#!/bin/bash
HOOK_FILE=".git/hooks/pre-commit"

cat > "$HOOK_FILE" << 'EOF'
#!/bin/bash
echo "Running pre-flight cognitive checks..."

# 1. Static Type Checking
echo "Executing mypy..."
uv run mypy seed_agent.py || { echo "Type check failed! Commit aborted."; exit 1; }

# 2. Logic Verification
echo "Executing pytest..."
uv run pytest tests/ || { echo "Tests failed! Commit aborted."; exit 1; }

echo "All checks passed. Memory committed."
EOF

chmod +x "$HOOK_FILE"
