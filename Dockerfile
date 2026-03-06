# Use Playwright base image to avoid missing browser dependencies
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUROBOROS_DRIVE_ROOT=/drive \
    OUROBOROS_REPO_DIR=/app \
    # Tell Playwright we already have the browsers
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install git and other basic utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies using uv system-wide
RUN uv pip install --system -e .

# Copy the rest of the application
COPY . .

# Set up git config for the agent (prevents git commit errors)
RUN git config --global user.name "Ouroboros" && \
    git config --global user.email "ouroboros@agent.local" && \
    git config --global init.defaultBranch ouroboros && \
    git config --global --add safe.directory /app

# The entrypoint launches the supervisor
CMD ["python", "-m", "supervisor.main"]
