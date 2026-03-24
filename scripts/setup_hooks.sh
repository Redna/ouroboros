#!/bin/bash
HOOK_FILE=".git/hooks/pre-commit"

cat > "$HOOK_FILE" << 'EOF'
#!/bin/bash
echo "Running pre-commit checks..."
# Run pytest. If it fails, abort the commit.
uv run pytest tests/ || { echo "Tests failed! Commit aborted."; exit 1; }
EOF

chmod +x "$HOOK_FILE"
