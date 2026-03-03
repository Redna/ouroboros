"""Ouroboros Supervisor package.

Loads .env from the repo root (if present) before anything else runs.
This means any variable in .env is available as os.environ for all
supervisor and ouroboros modules.
"""
import pathlib

# Load .env early — must happen before any env reads in this package.
try:
    from dotenv import load_dotenv
    _env_path = pathlib.Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass  # python-dotenv not installed — env vars must be set manually
