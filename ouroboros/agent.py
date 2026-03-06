from __future__ import annotations
import json
import logging
import os
import pathlib
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolRegistry, ToolContext
from ouroboros.memory import Memory
from ouroboros.loop import run_llm_loop
from ouroboros.context import build_llm_messages
from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, get_git_info, sanitize_task_for_event
)

log = logging.getLogger(__name__)

_worker_boot_logged = False
_worker_boot_lock = threading.Lock()

@dataclass
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / rel).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / rel).resolve()

class OuroborosAgent:
    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: Optional[int] = None
        self._current_task_type: Optional[str] = None

        # Message injection: owner can send messages while agent is busy
        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)

        self._log_worker_boot_once()

    def inject_message(self, text: str) -> None:
        """Thread-safe: inject owner message into the active conversation."""
        self._incoming_messages.put(text)

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            git_branch, git_sha = get_git_info(self.env.repo_dir)
            append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'worker_boot',
                'pid': os.getpid(), 'git_branch': git_branch, 'git_sha': git_sha,
            })
            self._verify_restart(git_sha)
            self._verify_system_state(git_sha)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _verify_restart(self, git_sha: str) -> None:
        """Best-effort restart verification."""
        try:
            pending_path = self.env.drive_path('state') / 'pending_restart_verify.json'
            claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")
            try:
                os.rename(str(pending_path), str(claim_path))
            except (FileNotFoundError, Exception):
                return
            try:
                claim_data = json.loads(read_text(claim_path))
                expected_sha = str(claim_data.get("expected_sha", "")).strip()
                ok = bool(expected_sha and expected_sha == git_sha)
                append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                    'ts': utc_now_iso(), 'type': 'restart_verify',
                    'pid': os.getpid(), 'ok': ok,
                    'expected_sha': expected_sha, 'observed_sha': git_sha,
                })
            except Exception:
                log.debug("Failed to log restart verify event", exc_info=True)
                pass
            try:
                claim_path.unlink()
            except Exception:
                log.debug("Failed to delete restart verify claim file", exc_info=True)
                pass
        except Exception:
            log.debug("Restart verification failed", exc_info=True)
            pass

    def _verify_system_state(self, git_sha: str) -> None:
        """Bible Principle 1: verify system state on every startup."""
        checks = {}
        issues = 0
        drive_logs = self.env.drive_path('logs')

        # 1. Uncommitted changes
        checks["uncommitted_changes"], issue_count = self._check_uncommitted_changes()
        issues += issue_count

        # 2. VERSION vs git tag
        checks["version_sync"], issue_count = self._check_version_sync()
        issues += issue_count

        # 3. Budget check
        checks["budget"], issue_count = self._check_budget()
        issues += issue_count

        # Log verification result
        event = {
            'ts': utc_now_iso(),
            'type': 'startup_verification',
            'checks': checks,
            'issues_count': issues,
            'git_sha': git_sha,
        }
        append_jsonl(drive_logs / 'events.jsonl', event)

        if issues > 0:
            log.warning(f"Startup verification found {issues} issue(s): {checks}")

    def _check_uncommitted_changes(self) -> Tuple[dict, int]:
        """Check for uncommitted changes and attempt auto-rescue commit & push."""
        import re
        import subprocess
        try:
            result = subprocess.run([
                "git", "status", "--porcelain"
            ], cwd=str(self.env.repo_dir), capture_output=True, text=True, timeout=10, check=True)
            dirty_files = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
            if dirty_files:
                # Auto-rescue: commit and push
                auto_committed = False
                try:
                    # Only stage tracked files (not secrets/notebooks)
                    subprocess.run([
                        "git", "add", "-u"
                    ], cwd=str(self.env.repo_dir), timeout=10, check=True)
                    subprocess.run([
                        "git", "commit", "-m", "auto-rescue: uncommitted changes detected on startup"
                    ], cwd=str(self.env.repo_dir), timeout=30, check=True)
                    # Validate branch name
                    if not re.match(r'^[a-zA-Z0-9_/-]+$', self.env.branch_dev):
                        raise ValueError(f"Invalid branch name: {self.env.branch_dev}")
                    # Pull with rebase before push
                    subprocess.run([
                        "git", "pull", "--rebase", "origin", self.env.branch_dev
                    ], cwd=str(self.env.repo_dir), timeout=60, check=True)
                    # Push
                    try:
                        subprocess.run([
                            "git", "push", "origin", self.env.branch_dev
                        ], cwd=str(self.env.repo_dir), timeout=60, check=True)
                        auto_committed = True
                        log.warning(f"Auto-rescued {len(dirty_files)} uncommitted files on startup")
                    except subprocess.CalledProcessError:
                        # If push fails, undo the commit
                        subprocess.run([
                            "git", "reset", "HEAD~1"
                        ], cwd=str(self.env.repo_dir), timeout=10, check=True)
                        raise
                except Exception as e:
                    log.warning(f"Failed to auto-rescue uncommitted changes: {e}", exc_info=True)
                return {
                    'status': 'warning', 'files': dirty_files[:20],
                    'auto_committed': auto_committed,
                }, 1
            else:
                return {'status': 'ok'}, 0
        except Exception as e:
            return {'status': 'error', 'error': str(e)}, 0

    def _check_version_sync(self) -> Tuple[dict, int]:
        """Check VERSION file sync with git tags and pyproject.toml."""
        import subprocess
        import re
        try:
            version_file = read_text(self.env.repo_path("VERSION")).strip()
            issue_count = 0
            result_data = {'version_file': version_file}

            # Check pyproject.toml version
            pyproject_path = self.env.repo_path("pyproject.toml")
            pyproject_content = read_text(pyproject_path)
            match = re.search(r'''^version\s*=\s*["']([^"']+)["']''', pyproject_content, re.MULTILINE)
            if match:
                pyproject_version = match.group(1)
                result_data["pyproject_version"] = pyproject_version
                if version_file != pyproject_version:
                    result_data["status"] = "warning"
                    issue_count += 1

            # Check README.md version (Bible P7: VERSION == README version)
            try:
                readme_content = read_text(self.env.repo_path("README.md"))
                readme_match = re.search(r'\*\*Version:\*\*\s*(\d+\.\d+\.\d+)', readme_content)
                if readme_match:
                    readme_version = readme_match.group(1)
                    result_data["readme_version"] = readme_version
                    if version_file != readme_version:
                        result_data["status"] = "warning"
                        issue_count += 1
            except Exception:
                log.debug("Failed to check README.md version", exc_info=True)

            # Check git tags
            result = subprocess.run([
                "git", "describe", "--tags", "--abbrev=0"
            ], cwd=str(self.env.repo_dir), capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                result_data["status"] = "warning"
                result_data["message"] = "no_tags"
                return result_data, issue_count
            else:
                latest_tag = result.stdout.strip().lstrip('v')
                result_data["latest_tag"] = latest_tag
                if version_file != latest_tag:
                    result_data["status"] = "warning"
                    issue_count += 1

            if issue_count == 0:
                result_data["status"] = "ok"

            return result_data, issue_count
        except Exception as e:
            return {'status': 'error', 'error': str(e)}, 0

    def _check_budget(self) -> Tuple[dict, int]:
        """Check budget remaining with warning thresholds."""
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            total_budget_str = os.environ.get("TOTAL_BUDGET", "")

            # Handle unset or zero budget gracefully
            if not total_budget_str or float(total_budget_str) == 0:
                return {'status': 'unconfigured'}, 0
            else:
                total_budget = float(total_budget_str)
                spent = float(state_data.get("spent_usd", 0))
                remaining = max(0, total_budget - spent)

                if remaining < 10:
                    status = "emergency"
                    issues = 1
                elif remaining < 50:
                    status = "critical"
                    issues = 1
                elif remaining < 100:
                    status = "warning"
                    issues = 0
                else:
                    status = "ok"
                    issues = 0

                return {
                    'status': status,
                    'remaining_usd': round(remaining, 2),
                    'total_usd': total_budget,
                    'spent_usd': round(spent, 2),
                }, issues
        except Exception as e:
            return {'status': 'error', 'error': str(e)}, 0

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Worker-compatible entry point: execute one task and return pending events."""
        self._busy = True
        self._task_started_ts = time.time()
        self._pending_events = []
        self._current_chat_id = task.get("chat_id")
        self._current_task_type = task.get("type")

        task_id = task.get("id", "unknown")

        try:
            # Prepare context for the task
            drive_logs = self.env.drive_path('logs')
            sanitized_task = sanitize_task_for_event(task, drive_logs)
            append_jsonl(drive_logs / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'task_received', 'task': sanitized_task
            })

            # Set tool context for this task
            ctx = ToolContext(
                repo_dir=self.env.repo_dir,
                drive_root=self.env.drive_root,
                branch_dev=self.env.branch_dev,
                pending_events=self._pending_events,
                current_chat_id=self._current_chat_id,
                current_task_type=self._current_task_type,
                emit_progress_fn=self._emit_progress,
                task_depth=int(task.get("depth", 0)),
                is_direct_chat=bool(task.get("_is_direct_chat")),
            )
            self.tools.set_context(ctx)

            # Build messages
            messages, _cap_info = build_llm_messages(self.env, self.memory, task)

            # Run the LLM loop
            final_text, usage, _trace = run_llm_loop(
                messages=messages,
                tools=self.tools,
                llm=self.llm,
                drive_logs=drive_logs,
                emit_progress=self._emit_progress,
                incoming_messages=self._incoming_messages,
                task_type=str(task.get("type", "task")),
                task_id=task_id,
                budget_remaining_usd=self._get_budget_remaining(),
                event_queue=self._event_queue,
                drive_root=self.env.drive_root,
            )

            # Final response to owner (if not direct chat)
            if not task.get("_is_direct_chat") and self._current_chat_id:
                self._pending_events.append({
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": final_text,
                })

            # Update journal
            self.memory.append_journal({
                "ts": utc_now_iso(),
                "type": "task_completed",
                "task_id": task_id,
                "usage": usage,
            })

        except Exception as e:
            log.error(f"Task {task_id} failed: {e}", exc_info=True)
            if self._current_chat_id:
                self._pending_events.append({
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": f"⚠️ Task error: {type(e).__name__}: {e}",
                })

        finally:
            self._busy = False
            self._current_chat_id = None
            self._current_task_type = None

        return self._pending_events

    def _emit_progress(self, message: str) -> None:
        """Emit progress messages to the owner."""
        now = time.time()
        # Rate limit progress messages to Telegram (once per 2s)
        if self._current_chat_id and (now - self._last_progress_ts > 2.0):
            self._pending_events.append({
                "type": "send_message",
                "chat_id": self._current_chat_id,
                "text": f"⚙️ {message}",
                "is_progress": True,
            })
            self._last_progress_ts = now
        
        # Always log to progress.jsonl
        append_jsonl(self.env.drive_path('logs') / 'progress.jsonl', {
            'ts': utc_now_iso(),
            'task_id': self._current_task_type or "unknown",
            'text': message
        })

    def _get_budget_remaining(self) -> Optional[float]:
        try:
            state_path = self.env.drive_path("state/state.json")
            if not state_path.exists():
                return None
            state_data = json.loads(read_text(state_path))
            total_budget = float(os.environ.get("TOTAL_BUDGET", "0"))
            if total_budget <= 0:
                return None
            spent = float(state_data.get("spent_usd", 0))
            return max(0, total_budget - spent)
        except Exception:
            return None

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    """Entry point for workers to create the agent."""
    env = Env(
        repo_dir=pathlib.Path(repo_dir).resolve(),
        drive_root=pathlib.Path(drive_root).resolve(),
        branch_dev=os.environ.get("OUROBOROS_BRANCH_DEV", "ouroboros"),
    )
    return OuroborosAgent(env, event_queue=event_queue)
