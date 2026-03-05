from __future__ import annotations
import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

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
            match = re.search(r'^version\s*=\s*\["\']([^\"']+)\["\']', pyproject_content, re.MULTILINE)
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

    def _handle_message(self, message: str) -> None:
        """Handle incoming messages from the owner."""
        self._incoming_messages.put(message)

    def _run_llm_loop(self, messages: List[Dict[str, Any]], task_id: str) -> Tuple[str, Dict[str, Any]]:
        """Run the LLM loop with messages and task ID."""
        # Call the LLM loop with messages (no incoming_message parameter)
        return run_llm_loop(messages, task_id)

    def _execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task with the given parameters."""
        # Build context for the task
        context = self._prepare_task_context(task)
        
        # Execute the task
        result = self._run_llm_loop(context['messages'], task['id'])
        
        # Update memory and return result
        self.memory.update(result)
        return result

    def _prepare_task_context(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare context for the task."""
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
        )

        return {
            'ctx': ctx,
            'messages': build_llm_messages(ctx, task),
            'task_id': task['id']
        }

    def _emit_progress(self, message: str) -> None:
        """Emit progress messages to the owner."""
        if self._event_queue:
            self._event_queue.put(message)

    def _log_task_progress(self, task_id: str, status: str, message: str) -> None:
        """Log task progress to the event queue."""
        append_jsonl(self.env.drive_path('logs') / 'progress.jsonl', {
            'ts': utc_now_iso(),
            'task_id': task_id,
            'status': status,
            'message': message
        })

    def _handle_task(self, task: Dict[str, Any]) -> None:
        """Handle a task from the event queue."""
        try:
            # Prepare context for the task
            context = self._prepare_task_context(task)
            
            # Run the LLM loop
            result = self._run_llm_loop(context['messages'], task['id'])
            
            # Update memory and log progress
            self.memory.update(result)
            self._log_task_progress(task['id'], 'completed', result)
        except Exception as e:
            self._log_task_progress(task['id'], 'error', str(e))
            log.error(f"Task {task['id']} failed: {e}", exc_info=True)

    def _start_worker(self) -> None:
        """Start the worker thread."""
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        """Main worker loop."""
        while True:
            try:
                # Wait for a task or message
                event = self._event_queue.get(timeout=5)
                
                # Handle the event
                if event.get('type') == 'task':
                    self._handle_task(event)
                elif event.get('type') == 'message':
                    self._handle_message(event['message'])
                else:
                    log.warning(f"Unknown event type: {event.get('type')}")
            except Exception as e:
                log.error(f"Worker loop error: {e}", exc_info=True)
                break

    def _worker_boot(self) -> None:
        """Worker boot process."""
        self._start_worker()

    def _run_llm_loop(self, messages: List[Dict[str, Any]], task_id: str) -> Tuple[str, Dict[str, Any]]:
        """Run the LLM loop with messages and task ID."""
        # Call the LLM loop with messages (no incoming_message parameter)
        return run_llm_loop(messages, task_id)

    def _execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task with the given parameters."""
        # Build context for the task
        context = self._prepare_task_context(task)
        
        # Execute the task
        result = self._run_llm_loop(context['messages'], task['id'])
        
        # Update memory and return result
        self.memory.update(result)
        return result

    def _prepare_task_context(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare context for the task."""
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
        )

        return {
            'ctx': ctx,
            'messages': build_llm_messages(ctx, task),
            'task_id': task['id']
        }