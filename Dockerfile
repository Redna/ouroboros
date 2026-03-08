# Use a more recent Playwright base image
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUROBOROS_DRIVE_ROOT=/drive \
    OUROBOROS_REPO_DIR=/app \
    # Tell Playwright where to look for browsers
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install git, GitHub CLI (gh), and other basic utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gosu sudo && \
    # Install GitHub CLI
    (type -p wget >/dev/null || (apt-get update && apt-get install wget -y)) \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install gh -y \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies using uv system-wide
RUN uv pip install --system -e .

# Ensure playwright browsers are installed in the expected path
RUN playwright install --with-deps chromium

# Copy the rest of the application
COPY . .

# Add the entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The entrypoint launches the supervisor
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "supervisor.main"]
