#!/bin/bash
set -e

# These can be passed via docker-compose.yml or 'docker run -e'
# Default to 1000:1000 which is common for the first user on Linux
USER_ID=${PUID:-1000}
GROUP_ID=${PGID:-1000}

# Create a group if it doesn't exist
if ! getent group "$GROUP_ID" >/dev/null; then
    groupadd -g "$GROUP_ID" ouroboros
fi

# Create a user if it doesn't exist
if ! getent passwd "$USER_ID" >/dev/null; then
    useradd -u "$USER_ID" -g "$GROUP_ID" -m -s /bin/bash ouroboros
fi

# Get the actual username for the UID
USER_NAME=$(getent passwd "$USER_ID" | cut -d: -f1)

# Ensure the drive root is owned by the user
# (It might be a Docker volume owned by root)
if [ -d "/drive" ]; then
    chown -R "$USER_ID:$GROUP_ID" /drive
fi

# Ensure Playwright browsers are accessible (read-only for non-root is usually enough, but let's be safe)
# PLAYWRIGHT_BROWSERS_PATH is set in Dockerfile to /ms-playwright
if [ -d "/ms-playwright" ]; then
    chmod -R 755 /ms-playwright
fi

# Configure git for the user if it's not already configured in their home
# This is important so the user can commit as "Ouroboros"
HOME_DIR=$(getent passwd "$USER_ID" | cut -d: -f6)
sudo -u "$USER_NAME" -H git config --global user.name "Ouroboros"
sudo -u "$USER_NAME" -H git config --global user.email "ouroboros@agent.local"
sudo -u "$USER_NAME" -H git config --global init.defaultBranch ouroboros
sudo -u "$USER_NAME" -H git config --global --add safe.directory /app

# Drop privileges and execute the command
echo "Starting Ouroboros as $USER_NAME ($USER_ID:$GROUP_ID)..."
exec gosu "$USER_NAME" "$@"
