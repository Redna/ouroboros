import os
import sys
import json
import time
import subprocess
import requests
import re
import ast
import tempfile
import shutil
import traceback
import fcntl
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Query, QueryCursor
from openai import OpenAI

import constants
import agent_state
import llm_interface
import comms

# Initialize Tree-sitter parser and query for repository mapping
PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

MAP_QUERY = Query(PY_LANGUAGE, """
    (class_definition name: (identifier) @class.name)
    (function_definition name: (identifier) @function.name)
""")


def _resolve_safe_path(raw_path: str) -> Path:
    """Resolves path and enforces boundary guards (constants.ROOT_DIR or constants.MEMORY_DIR).

    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = constants.ROOT_DIR / p

    try:
        p = p.resolve(strict=True)
    except FileNotFoundError:
        p = p.parent.resolve(strict=True) / p.name

    if not str(p).startswith(str(constants.ROOT_DIR)) and not str(p).startswith(str(constants.MEMORY_DIR)):
        raise PermissionError(f"Target must be within {constants.ROOT_DIR} or {constants.MEMORY_DIR}.")

    return p

def _validate_python_syntax(content: str) -> None:
    """Fast-fail check for Python syntax errors."""
    ast.parse(content)

def _normalize_text(text: str) -> str:
    """Normalize line endings and strip trailing whitespace for resilient matching."""
    return "\n".join([line.rstrip() for line in text.replace("\r\n", "\n").splitlines()])


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, description: str, parameters: dict, bucket: str = "global"):
        """Decorator to register a tool using the function's own name."""
        def decorator(func):
            tool_name = func.__name__
            self.tools[tool_name] = {
                "desc": description,
                "params": parameters,
                "handler": func,
                "bucket": bucket
            }
            return func
        return decorator

    def get_names(self, allowed_buckets=None):
        return [n for n, t in self.tools.items() if allowed_buckets is None or t["bucket"] in allowed_buckets]

    def get_specs(self, allowed_buckets=None):
        return [
            {"type": "function", "function": {"name": n, "description": t["desc"], "parameters": t["params"]}}
            for n, t in self.tools.items()
            if allowed_buckets is None or t["bucket"] in allowed_buckets
        ]

    def execute(self, name, args, call_id=None):
        if name not in self.tools:
            return f"Error: Tool '{name}' not found."
        try:
            handler = self.tools[name]["handler"]
            result = handler(args)
            return llm_interface.redact_secrets(str(result))
        except Exception as e:
            return llm_interface.redact_secrets(f"Error executing {name}: {e}")

registry = ToolRegistry()

@registry.tool(
    description="Execute shell command.",
    parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    bucket="bash"
)
def bash_command(args):
    command = args.get("command", "")
    try:
        r = subprocess.run(command, shell=True, cwd=str(constants.ROOT_DIR), capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        if out and len(out) > constants.BASH_OUTPUT_MAX_CHARS:
            warning = f"\n\n[SYSTEM WARNING: Output truncated! The command returned too much data. Use 'grep', 'head', 'tail', or exclude directories like 'venv'/'.git' to filter results.]"
            return out[:constants.BASH_OUTPUT_MAX_CHARS] + warning
        return out if out else f"Success. (Exit Code: {r.returncode}, No Output)"
    except subprocess.TimeoutExpired:
        return "[SYSTEM WARNING: Command timed out after 60 seconds. It may be hanging, requiring interactive input, or processing too much data. Run background tasks with '&' or fix the command.]"
    except Exception as e:
        return f"Error: {e}"

@registry.tool(
    description="Run the test suite to verify code changes before committing. Use this after modifying code and BEFORE committing.",
    parameters={
        "type": "object",
        "properties": {
            "test_path": {"type": "string", "description": "Optional specific test file to run (e.g., 'tests/test_core.py'). Leave empty to run the full suite."}
        }
    },
    bucket="bash"
)
def run_tests(args: dict) -> str:
    """Executes pytest via uv. High-signal verification for self-evolution."""
    test_path = args.get("test_path", "tests/")
    try:
        # Run pytest via uv to match the pre-commit environment
        result = subprocess.run(
            f"uv run pytest {test_path} -v --tb=short",
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")

        if result.returncode == 0:
            # For success, keep it dense
            return f"✅ All tests passed successfully.\n{output[-500:]}"

        # For failure, truncate massive tracebacks but keep the summary
        if len(output) > 2000:
            output = output[:1000] + "\n\n... [TRUNCATED] ...\n\n" + output[-1000:]

        return f"❌ TESTS FAILED. You must fix the code or update the tests before committing.\n\n{output}"

    except subprocess.TimeoutExpired:
        return "❌ TESTS FAILED: Pytest execution timed out. You may have introduced an infinite loop."
    except Exception as e:
        return f"SYSTEM ERROR running tests: {e}"

@registry.tool(
    description="Overwrite file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    bucket="filesystem"
)
def write_file(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        content = args.get("content", "")
        if p.suffix == ".py":
            try:
                _validate_python_syntax(content)
            except SyntaxError as e:
                return f"SYSTEM REJECTED: Invalid Python syntax in content.\nError: {e.msg} at line {e.lineno}\nTraceback: {traceback.format_exc()}\nFix syntax and try again."

        Path(p.parent).mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=p.parent, text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        shutil.move(temp_path, p)
        return f"Success: Safely wrote and validated {p.name}."
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error writing file: {e}"

@registry.tool(
    description="Surgical edit. Replaces a specific block of text in a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "search_text": {"type": "string"}, "replace_text": {"type": "string"}}, "required": ["path", "search_text", "replace_text"]},
    bucket="filesystem"
)
def patch_file(args):
    try:
        file_path = _resolve_safe_path(args.get("path", ""))
        if not file_path.exists() or not file_path.is_file():
            return f"Error: File '{file_path.name}' does not exist."

        search_text = args.get("search_text", "")
        replace_text = args.get("replace_text", "")
        content = file_path.read_text(encoding="utf-8")

        norm_content = _normalize_text(content)
        norm_search = _normalize_text(search_text)

        occurrence_count = norm_content.count(norm_search)
        if occurrence_count == 0:
            # WP: Actionable Feedback for Failed Patches
            lines = content.splitlines()
            search_lines = search_text.splitlines()

            error_msg = f"Error: Exact `search_text` not found in {file_path.name}.\n"
            error_msg += "This is usually caused by incorrect leading spaces or missing blank lines.\n"

            # Find potential matches by looking for the first non-empty line of the search block
            first_search_line = next((l.strip() for l in search_lines if l.strip()), None)
            potential_matches = []

            if first_search_line:
                for i, line in enumerate(lines):
                    if first_search_line in line:
                        start = max(0, i - 2)
                        end = min(len(lines), i + len(search_lines) + 2)
                        snippet = "\n".join(lines[start:end])
                        potential_matches.append(snippet)

            if potential_matches:
                error_msg += "\nDid you mean to target this section? Pay close attention to the indentation:\n"
                error_msg += "```python\n" + potential_matches[0] + "\n```\n"
                error_msg += "Adjust your `search_text` to match the file exactly and try again."
            else:
                error_msg += "Could not find any lines matching the start of your search block. Use `read_file` to check the current file contents."

            return error_msg
        elif occurrence_count > 1:
            return f"Error: 'search_text' appears {occurrence_count} times. Please provide a more unique block."

        new_content = norm_content.replace(norm_search, replace_text)
        if file_path.suffix == ".py":
            try:
                _validate_python_syntax(new_content)
            except SyntaxError as e:
                return f"SYSTEM REJECTED: Patch creates invalid Python syntax.\nError: {e.msg} at line {e.lineno}\nTraceback: {traceback.format_exc()}\nFix your indentation or logic."

        file_path.write_text(new_content, encoding="utf-8")
        return f"Success: Surgically patched and validated {file_path.name}."
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error patching file: {e}"

@registry.tool(
    description="Read file contents (e.g., read /memory/insights.md).",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]},
    bucket="memory_access"
)
def read_file_tool(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        if not p.exists() or not p.is_file():
            return f"Error: File '{p.name}' does not exist or is a directory."

        content_lines = p.read_text(encoding="utf-8").splitlines()
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if start_line is not None or end_line is not None:
            s = (max(1, int(start_line)) - 1) if start_line is not None else 0
            e = int(end_line) if end_line is not None else len(content_lines)
            content_lines = content_lines[s:e]
            prefix = f"[Showing lines {s+1} to {e} of {len(content_lines) + s}]\n"
        else:
            prefix = ""

        content = "\n".join(content_lines)
        if len(content) > constants.READ_FILE_MAX_CHARS:
            warning = f"\n\n[SYSTEM WARNING: File too large. Truncated to {constants.READ_FILE_MAX_CHARS} chars. Use start_line/end_line.]"
            return prefix + content[:constants.READ_FILE_MAX_CHARS] + warning
        return prefix + content
    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error reading file: {e}"


@registry.tool(
    description="Generate a high-signal structural map of the Python codebase using Tree-sitter AST parsing.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path to map (e.g., '.'). Defaults to project root."}
        }
    },
    bucket="filesystem"
)
def generate_repo_map(args: dict) -> str:
    target_path = args.get("path", ".")
    try:
        root = _resolve_safe_path(target_path)
    except Exception as e:
        return f"Error: {e}"

    if not root.is_dir():
        return f"Error: '{target_path}' is not a directory."

    skeleton = []

    for current_root, dirs, files in os.walk(root):
        # Skip hidden directories and virtual environments
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("venv", "node_modules", "__pycache__", ".venv")]

        for file in files:
            if file.endswith(".py"):
                file_path = Path(current_root) / file
                try:
                    rel_path = file_path.relative_to(constants.ROOT_DIR)
                except ValueError:
                    rel_path = file_path # Fallback if not relative to root

                try:
                    source_code = file_path.read_bytes()
                    tree = parser.parse(source_code)

                    cursor = QueryCursor(MAP_QUERY)
                    captures_dict = cursor.captures(tree.root_node)

                    if captures_dict:
                        # Extract all captures and sort them by start byte to maintain order
                        captures = []
                        for capture_name, nodes in captures_dict.items():
                            for node in nodes:
                                captures.append((node, capture_name))
                        captures.sort(key=lambda x: x[0].start_byte)

                        skeleton.append(f"File: {rel_path}")
                        for node, capture_name in captures:
                            if node.text is None:
                                continue
                            text = node.text.decode('utf8')
                            if capture_name == "class.name":
                                skeleton.append(f"  class {text}:")
                            elif capture_name == "function.name":
                                skeleton.append(f"    def {text}(...):")
                except Exception as e:
                    skeleton.append(f"File: {rel_path}\n  [Parse Error: {e}]")

    return "\n".join(skeleton) or "No Python files found or mapped."


@registry.tool(
    description="Compress the active execution log by amputating the middle history (the 'Belly'). You MUST provide a dense, factual synthesis of the dropped history. This synthesis is stored natively in this tool call's arguments, maintaining an unbroken, single timeline. Always use `store_memory` BEFORE calling this if there are facts that must survive permanently.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Current task ID (e.g. 'global_trunk')."},
            "synthesis": {"type": "string", "description": "Dense summary following the DELTA PATTERN: 1. State Delta (what changed), 2. Negative Knowledge (what failed), 3. Handoff (exact next step)."},
            "drop_turns": {"type": "integer", "description": "Optional: Number of recent tool/assistant turns to delete. If omitted or 0, folds ALL history except the initial objective."}
        },
        "required": ["task_id", "synthesis"]
    },
    bucket="context_control"
)
def fold_context(args: dict) -> str:
    task_id = args.get("task_id", "global_trunk")
    synthesis = args.get("synthesis")
    try:
        drop_turns = int(args.get("drop_turns", 0))
    except (ValueError, TypeError):
        return "Error: 'drop_turns' must be an integer."

    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists():
        return f"Error: Task log '{task_id}' not found."

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        return f"Error reading log: {e}"

    if len(messages) <= 4:
        return f"Error: Not enough history to fold safely. Context is already minimal."

    # Define the Head: The first 2 messages (Genesis User + First Assistant)
    head_size = 2
    head = messages[:head_size]

    if drop_turns == 0:
        # Amputate everything except the Head.
        preserved = head
        turns_dropped = (len(messages) - head_size) // 2
    else:
        # Keep Head, Amputate `drop_turns * 2` messages from the END of the current log file
        messages_to_drop = drop_turns * 2
        cutoff = max(head_size, len(messages) - messages_to_drop)
        preserved = messages[:cutoff]
        turns_dropped = (len(messages) - cutoff) // 2

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            for msg in preserved:
                f.write(json.dumps(msg) + "\n")

        # Update metrics
        state = agent_state.load_state()
        state["trunk_turns"] = max(1, state.get("trunk_turns", 1) - turns_dropped)
        agent_state.save_state(state)

    except Exception as e:
        return f"Error writing log during fold: {e}"

    return f"Fold successful. {turns_dropped} turns (the 'Belly') amputated. Synthesis anchored in this tool call. Context reset."


@registry.tool(
    description="Send a Telegram message to the creator. Use this for reporting progress, providing results of an interrupt, or when verbal peer-to-peer communication is required.",
    parameters={"type": "object", "properties": {"chat_id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["text"]},
    bucket="global"
)
def send_telegram_message(args):
    state = agent_state.load_state()
    chat_id = args.get("chat_id") or state.get("creator_id")
    text = args.get("text")
    if not chat_id: return "Error: No chat_id provided and no creator registered."
    if not constants.TELEGRAM_BOT_TOKEN: return "Error: constants.TELEGRAM_BOT_TOKEN not set."

    try:
        r = requests.post(f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code == 200:
            agent_state.append_chat_history("Ouroboros", text)
            return "Message sent successfully."
        return f"Telegram Error {r.status_code}: {r.text}"
    except Exception as e: return f"Telegram failure: {e}"

def _acquire_queue_lock(file_path: Path, timeout: float = 5.0) -> Optional[int]:
    """Acquire exclusive lock on file. Returns fd or None on timeout."""
    try:
        fd = os.open(str(file_path), os.O_RDWR | os.O_CREAT, 0o644)
        start_time = time.time()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (IOError, OSError):
                if time.time() - start_time > timeout:
                    os.close(fd)
                    return None
                time.sleep(0.1)
    except Exception:
        return None


def _release_queue_lock(fd: int) -> None:
    """Release file lock and close descriptor."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


def _is_semantic_duplicate(new_desc: str, existing_queue: List[Dict]) -> bool:
    """Detect semantic duplicates to prevent cognitive loops.

    Uses multi-tier detection:
    1. Exact match (normalized)
    2. High keyword overlap (>50% of significant words)
    3. Same action + same target pattern
    """
    stop_words = {'the', 'a', 'an', 'to', 'for', 'in', 'on', 'at', 'by', 'with', 'and', 'or'}
    action_words = {'fix', 'update', 'modify', 'change', 'refactor', 'improve', 'optimize', 'implement', 'add', 'remove'}

    def extract_significant_words(text: str) -> set:
        words = set(text.lower().split())
        return words - stop_words

    new_keywords = extract_significant_words(new_desc)
    if len(new_keywords) < 3:  # Too short to be meaningful
        return False

    for task in existing_queue:
        existing_desc = task.get("description", "")
        existing_keywords = extract_significant_words(existing_desc)

        if len(existing_keywords) < 3:
            continue

        # Tier 1: High overlap ratio (>50% of smaller set matches)
        overlap = new_keywords & existing_keywords
        min_len = min(len(new_keywords), len(existing_keywords))
        if len(overlap) >= 3 and len(overlap) / min_len > 0.5:
            return True

        # Tier 2: Same action words + same target words
        new_actions = new_keywords & action_words
        existing_actions = existing_keywords & action_words
        if new_actions and existing_actions and (new_actions & existing_actions):
            new_target = new_keywords - action_words
            existing_target = existing_keywords - action_words
            target_overlap = new_target & existing_target
            if len(target_overlap) >= 2:
                return True

    return False


def _run_git_command(args: List[str]) -> Tuple[int, str, str]:
    """Run git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Git command timed out"
    except Exception as e:
        return -1, "", str(e)


@registry.tool(
    description="Commit staged changes to git with a message. Runs pre-commit hooks automatically.",
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"}
        },
        "required": ["message"]
    },
    bucket="global"
)
def git_commit(args):
    message = args.get("message", "").strip()
    if not message:
        return "Error: Commit message is required."

    # Check if there are staged changes
    rc, stdout, stderr = _run_git_command(["diff", "--staged", "--quiet"])
    if rc == 0:
        return "No staged changes to commit."

    # Run commit (pre-commit hooks run automatically)
    rc, stdout, stderr = _run_git_command(["commit", "-m", message])
    if rc != 0:
        return f"Commit failed: {stderr.strip()}"

    # Get commit hash
    _, short_hash, _ = _run_git_command(["rev-parse", "--short", "HEAD"])
    return f"Committed changes: {short_hash.strip()}\n{message}"


@registry.tool(
    description="Push committed changes to the remote repository.",
    parameters={
        "type": "object",
        "properties": {
            "remote": {"type": "string", "description": "Remote name (default: origin)", "default": "origin"},
            "branch": {"type": "string", "description": "Branch name (optional, uses current branch if not specified)"}
        }
    },
    bucket="global"
)
def git_push(args):
    remote = args.get("remote", "origin")
    branch = args.get("branch")

    cmd = ["push", remote]
    if branch:
        cmd.append(branch)

    rc, stdout, stderr = _run_git_command(cmd)
    if rc != 0:
        return f"Push failed: {stderr.strip()}"

    return f"Successfully pushed to {remote}{f'/{branch}' if branch else ''}."


@registry.tool(
    description="Remove a completed or obsolete task from the queue. Trunk uses this after handling an interrupt or administrative task. NEVER use this on 'global_trunk'.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "synthesis": {"type": "string", "description": "Brief summary of how it was handled."}
        },
        "required": ["task_id", "synthesis"]
    },
    bucket="global"
)
def dismiss_queue_item(args):
    task_id = args.get("task_id")
    synthesis = args.get("synthesis", "No synthesis provided.")

    if task_id == "global_trunk":
        return "Error: Cannot dismiss the global trunk."

    agent_state.append_task_archive(task_id, synthesis)

    q = agent_state.load_task_queue()
    q = [t for t in q if t.get("task_id") != task_id]
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2), encoding="utf-8")

    return f"Task {task_id} dismissed from queue."

@registry.tool(
    description="Queue a task, optionally scheduling it for the future. Omit run_after_timestamp for immediate queueing. Provide a UNIX timestamp to defer activation until that time.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "priority": {"type": "integer"},
            "parent_task_id": {"type": "string"},
            "context_notes": {"type": "string"},
            "run_after_timestamp": {"type": "number", "description": "Optional UNIX timestamp. If provided, the task sleeps until this time before becoming active."}
        },
        "required": ["description"]
    },
    bucket="global"
)
def push_task(args):
    description = args.get("description", "").strip()
    priority = args.get("priority", 1)
    run_after = args.get("run_after_timestamp")

    # File locking for scheduled tasks
    if run_after is not None:
        try:
            run_after = float(run_after)
        except (ValueError, TypeError):
            return "Error: 'run_after_timestamp' must be a valid UNIX timestamp."

        fd = _acquire_queue_lock(constants.SCHEDULED_TASKS_PATH)
        if fd is None:
            return "Error: Could not acquire lock on scheduled tasks file."
        try:
            scheduled = []
            if constants.SCHEDULED_TASKS_PATH.exists():
                try:
                    content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
                    if content: scheduled = json.loads(content)
                except Exception:
                    pass
            if any(t.get("description") == description and t.get("run_after") == run_after for t in scheduled):
                return "Error: An identical task is already scheduled for that exact time."
            tid = f"task_future_{int(time.time())}"
            scheduled.append({"task_id": tid, "description": description, "priority": priority, "run_after": run_after, "turn_count": 0})
            constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(scheduled, indent=2), encoding="utf-8")
        finally:
            _release_queue_lock(fd)
        time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run_after))
        return f"Scheduled {tid} to activate after {time_str}."

    # File locking for regular task queue
    fd = _acquire_queue_lock(constants.TASK_QUEUE_PATH)
    if fd is None:
        return "Error: Could not acquire lock on task queue."

    try:
        q = agent_state.load_task_queue()

        # Check for exact duplicates (normalized)
        normalized_desc = description.lower()
        if any(t.get("description", "").strip().lower() == normalized_desc for t in q):
            return "Error: A task with a similar description already exists in your queue. (Agency P0: Duplicate task skipped to avoid token waste P6)."

        # Check for semantic duplicates to prevent cognitive loops
        if _is_semantic_duplicate(description, q):
            return "Error: This task appears semantically similar to an existing queued task. Skipping to prevent cognitive loop."

        tid = f"task_{int(time.time())}"
        parent_id = args.get("parent_task_id")
        context_notes = args.get("context_notes", "")
        task_obj = {"task_id": tid, "description": description, "priority": priority, "turn_count": 0, "context_notes": context_notes}
        if parent_id: task_obj["parent_task_id"] = parent_id
        q.append(task_obj)
        q.sort(key=lambda x: x.get("priority", 1), reverse=True)
        constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))
        return f"Queued {tid} with priority {priority}."
    finally:
        _release_queue_lock(fd)

@registry.tool(
    description="Complete and archive the active task. Use this ONLY when the objective is 100% met.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "synthesis": {"type": "string", "description": "autopsy following the DELTA PATTERN: 1. State Delta, 2. Negative Knowledge, 3. Handoff."}
        },
        "required": ["task_id", "synthesis"]
    },
    bucket="global"
)
def complete_task(args):
    task_id = args.get("task_id", "global_trunk")
    synthesis = args.get("synthesis", "No synthesis provided.")
    agent_state.append_task_archive(task_id, synthesis)

    q = agent_state.load_task_queue()
    q = [t for t in q if t.get("task_id") != task_id]
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    state = agent_state.load_state()
    for key in ["sys_temp", "sys_think", f"partial_state_{task_id}"]:
        if key in state: del state[key]
    agent_state.save_state(state)

    return f"Task {task_id} completed and removed from queue. Synthesis archived."

@registry.tool(
    description="Suspend the active task. Use this when blocked, hitting token limits, or needing Trunk-level orchestration. Progress is saved via partial_state.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "synthesis": {"type": "string", "description": "Pause summary following the DELTA PATTERN."},
            "partial_state": {"type": "string", "description": "Technical context (variables, line numbers) needed to resume."}
        },
        "required": ["task_id", "synthesis"]
    },
    bucket="global"
)
def suspend_task(args):
    task_id = args.get("task_id", "global_trunk")
    synthesis = args.get("synthesis", "No synthesis provided.")
    partial_state = args.get("partial_state", "")

    q = agent_state.load_task_queue()
    for t in q:
        if t.get("task_id") == task_id:
            t["status"] = "SUSPENDED"
            t["partial_state"] = partial_state
            break
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    state = agent_state.load_state()
    state[f"partial_state_{task_id}"] = partial_state
    agent_state.save_state(state)

    return f"Task {task_id} suspended. State saved."

@registry.tool(
    description="Update working memory.",
    parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}},
    bucket="global"
)
def update_state_variable(args):
    key, value = args.get("key"), args.get("value")
    if not key or value is None: return "Error: 'key' and 'value' required."
    try:
        state = {}
        if constants.WORKING_STATE_PATH.exists():
            content = constants.WORKING_STATE_PATH.read_text(encoding="utf-8").strip()
            if content: state = json.loads(content)
        state[key] = value
        constants.WORKING_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return f"Working state successfully updated: '{key}' = '{value}'"
    except Exception as e: return f"Error saving state: {e}"

@registry.tool(
    description="Adjust LLM hyperparameters.",
    parameters={"type": "object", "properties": {"temperature": {"type": "number"}, "enable_thinking": {"type": "boolean"}}},
    bucket="global"
)
def set_cognitive_parameters(args):
    try:
        temp, think = args.get("temperature"), args.get("enable_thinking")
        state = agent_state.load_state()
        updates = []
        if temp is not None:
            state["sys_temp"] = float(temp)
            updates.append(f"Temperature={temp}")
        if think is not None:
            state["sys_think"] = bool(think)
            updates.append(f"Thinking={think}")
        agent_state.save_state(state)
        return "Cognitive parameters updated: " + ", ".join(updates)
    except Exception as e: return f"Error setting cognitive parameters: {e}"

@registry.tool(
    description="Local SearXNG search.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    bucket="search"
)
def web_search(args):
    query = args.get("query")
    if not constants.SEARXNG_URL: return "Error: constants.SEARXNG_URL not set."
    try:
        r = requests.get(f"{constants.SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=15)
        results = r.json().get("results", [])
        return "\n".join([f"- {res['title']}: {res['url']}\n  {res.get('content', '')[:200]}" for res in results[:5]]) or "No results found."
    except Exception as e: return f"Search error: {e}"

@registry.tool(
    description="Download URL to Markdown.",
    parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    bucket="search"
)
def fetch_webpage(args):
    url = args.get("url")
    if not url: return "Error: No URL provided."
    try:
        import trafilatura # type: ignore
        print(f"[System] Downloading clean markdown locally for: {url}")

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return f"Error: Could not download {url}. The site might be blocking crawlers or requires JavaScript."

        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=True,
            include_formatting=True
        )

        if not text:
            return "Error: Page fetched, but no readable article text was found."

        cache_dir = constants.MEMORY_DIR / "web_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', url.split('//')[-1])[:50]
        file_name = f"{int(time.time())}_{safe_name}.md"
        file_path = cache_dir / file_name

        file_path.write_text(text, encoding="utf-8")
        line_count = len(text.splitlines())

        return f"Success: Webpage downloaded and converted to Markdown.\nSaved to: {file_path}\nTotal Lines: {line_count}\n\nAction Required: Use the 'read_file' tool with 'start_line' and 'end_line' to read this file progressively (e.g., 500 lines at a time)."
    except ImportError:
        return "SYSTEM ERROR: 'trafilatura' library not installed. Please run 'pip install trafilatura'."
    except Exception as e:
        return f"Failed to fetch webpage locally: {e}"

@registry.tool(
    description="Save compute resources.",
    parameters={"type": "object", "properties": {"duration_seconds": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["duration_seconds"]},
    bucket="global"
)
def hibernate(args):
    try:
        duration = args.get("duration_seconds", 300)
        reason = args.get("reason", "No reason provided.")
        duration = min(int(duration), constants.MAX_HIBERNATE_SECONDS)
        state = agent_state.load_state()
        state["wake_time"] = time.time() + duration
        if "sys_temp" in state: del state["sys_temp"]
        if "sys_think" in state: del state["sys_think"]
        agent_state.save_state(state)
        print(f"[System] Agent elected to hibernate for {duration}s. Reason: {reason}")
        return f"SYSTEM_SIGNAL_HIBERNATE:{duration}"
    except Exception as e: return f"Error setting sleep cycle: {e}"

@registry.tool(
    description="Overwrite or synthesize a memory file with new content (dense summary or refactored text).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "is_jsonl": {"type": "boolean", "description": "Set to true if targeting a .jsonl task log to wrap content in a system message."}
        },
        "required": ["path", "content"]
    },
    bucket="memory_access"
)
def rewrite_memory(args):
    try:
        p = _resolve_safe_path(args.get("path", ""))
        content = args.get("content", "").strip()
        if len(content) < constants.MIN_REWRITE_CONTENT_LEN:
            return f"Error: Content too short (<{constants.MIN_REWRITE_CONTENT_LEN}). Provide full synthesized text."

        protected = ["agent_memory.json", "task_queue.json", ".agent_state.json", "task_archive.jsonl"]
        if p.name in protected:
            return f"Error: {p.name} is managed by dedicated tools. Use the appropriate tool to update."

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if args.get("is_jsonl") or p.suffix == ".jsonl":
            wrapped = {"role": "user", "content": f"--- COMPRESSED LOG ({timestamp}) ---\n{content}"}
            p.write_text(json.dumps(wrapped) + "\n", encoding="utf-8")
        else:
            p.write_text(f"--- SYNTHESIZED ({timestamp}) ---\n{content}\n", encoding="utf-8")
        return f"Successfully rewrote {p.name}."
    except Exception as e: return f"Error rewriting memory: {e}"

@registry.tool(
    description="Search /memory volume.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    bucket="memory_access"
)
def search_memory_archive(args):
    query = args.get("query", "")
    if not query: return "Error: No query provided."
    try:
        r = subprocess.run(
            ["grep", "-rEi", query, "/memory/"],
            capture_output=True, text=True, timeout=30
        )
        out = r.stdout + r.stderr
        return out[:4000] if out else "No matches found in memory."
    except subprocess.TimeoutExpired:
        return "Error: Memory search timed out after 30 seconds. Your query might be too broad or the memory volume is too large."
    except Exception as e:
        return f"Search error: {e}"

@registry.tool(
    description="Store or update a persistent memory. Key is a short topic sentence (your memory index shows all keys every turn). Content is the detailed knowledge. Use to record insights, task outcomes, learned patterns, or any important context for future recall. During P9 synthesis, merge related memories into higher-order entries.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short topic sentence (max 100 chars). Appears in your memory index."},
            "content": {"type": "string", "description": "Detailed knowledge or context for this topic."}
        },
        "required": ["key", "content"]
    },
    bucket="memory_access"
)
def store_memory(args):
    key = args.get("key", "").strip()
    content = args.get("content", "").strip()
    if not key or not content:
        return "Error: Both key and content are required."
    if len(key) > constants.MEMORY_KEY_MAX_LEN:
        return f"Error: Key must be <= {constants.MEMORY_KEY_MAX_LEN} characters. Shorten your topic sentence."
    return agent_state.store_memory_entry(key, content)

@registry.tool(
    description="Retrieve the detailed content of a specific memory by its key. Use when you need the full context behind a memory index entry.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The exact key or a substring to search for."}
        },
        "required": ["key"]
    },
    bucket="memory_access"
)
def recall_memory(args):
    key = args.get("key", "").strip()
    if not key:
        return "Error: Provide a key or substring to search for."
    result = agent_state.load_memory_entry(key)
    return result if result else f"No memory found matching '{key}'."

@registry.tool(
    description="Remove a memory entry to free a slot. Use when a memory is obsolete, or to make room before storing a new one. During P9 synthesis, forget low-value entries after merging their essence into higher-order memories.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "The exact key of the memory to remove."}
        },
        "required": ["key"]
    },
    bucket="memory_access"
)
def forget_memory(args):
    key = args.get("key", "").strip()
    if not key:
        return "Error: Provide the exact key to forget."
    return agent_state.forget_memory_entry(key)

@registry.tool(
    description="Reflect on the current state, progress, or blockers. Use this tool when you need to think, pause, or if you are stuck and need to break a cycle of failing tool calls. This satisfies the forced tool usage requirement without mutating the environment.",
    parameters={
        "type": "object",
        "properties": {
            "reflection": {"type": "string", "description": "Internal monologue, synthesis of findings, or reasoning about the current situation."},
            "status": {"type": "string", "description": "Optional status update (e.g., 'continuing', 'stuck', 'pivoting')."}
        },
        "required": ["reflection"]
    },
    bucket="system_control"
)
def reflect(args: dict) -> str:
    """Satisfy forced tool usage while allowing the agent to think."""
    reflection = args.get("reflection", "")
    status = args.get("status", "continuing")
    return f"Reflection logged. System status: {status}. You may proceed with the next action."


@registry.tool(
    description="Signal the watchdog to restart the agent process. Use this AFTER committing your changes via bash_command('git commit'). The git pre-commit hook enforces mypy and pytest automatically — if those fail, the commit (and therefore this restart) will be blocked.",
    parameters={"type": "object", "properties": {}},
    bucket="system_control"
)
def request_restart(args):
    return "SYSTEM_SIGNAL_RESTART"

@registry.tool(
    description="Query the gateway to discover available local and external cognitive engines and check the financial budget.",
    parameters={"type": "object", "properties": {}},
    bucket="system_control"
)
def check_environment(args):
    try:
        r = requests.get(f"{constants.API_BASE.replace('/v1', '')}/v1/environment", timeout=15)
        if r.status_code == 200:
            return json.dumps(r.json(), indent=2)
        else:
            return f"Error checking environment: {r.status_code} - {r.text}"
    except Exception as e:
        return f"Check environment failed: {e}"




def build_dynamic_telemetry_message(state: Dict[str, Any], queue: List[Dict[str, Any]]) -> str:
    """Generates the minimalist dynamic telemetry (HUD) as a User message."""
    token_limit = constants.CONTEXT_WINDOW
    current_context = state.get("last_context_size", 0)
    context_pct = int((current_context / token_limit) * 100) if token_limit else 0
    queue_len = len(queue)

    hud = f"[HUD | Context: {context_pct}% | Queue: {queue_len}]"
    return hud

def build_static_system_prompt(active_tool_specs: List[Dict[str, Any]]) -> str:
    identity = (constants.ROOT_DIR / "identity.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "identity.md").exists() else ""
    constitution = (constants.ROOT_DIR / "CONSTITUTION.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "CONSTITUTION.md").exists() else ""

    return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{constitution}

## STREAM OF CONSCIOUSNESS DIRECTIVES
1. You operate in a singular, continuous timeline. There are no branches. 
2. Act on initiative (P0). Address the top item in your Queue.
3. Your HUD tells you your physical context limit. Use `fold_context` when it gets high.
4. If a creator message interrupts you, suspend your thought, address it, and resume.
"""

def build_telemetry_piggyback(state: Dict[str, Any], queue: List[Dict[str, Any]]) -> str:
    """Generates the minimalist HUD to be appended to tool responses."""
    telemetry = build_dynamic_telemetry_message(state, queue)
    return f"\n\n{telemetry}"




def process_scheduled_tasks(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not constants.SCHEDULED_TASKS_PATH.exists():
        return queue
    try:
        content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return queue

        scheduled = json.loads(content)
        now = time.time()
        due_tasks = [t for t in scheduled if now >= t.get("run_after", 0)]

        if due_tasks:
            pending_tasks = [t for t in scheduled if now < t.get("run_after", 0)]
            constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(pending_tasks, indent=2), encoding="utf-8")

            for t in due_tasks:
                t.pop("run_after", None)
                queue.append(t)

            queue.sort(key=lambda x: x.get("priority", 1), reverse=True)

            # FIX: Explicitly save the active queue to disk here so comms.py reads the fresh state
            constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            print(f"[Scheduler] Temporal shift: {len(due_tasks)} scheduled tasks moved to active queue.")
    except Exception as e:
        print(f"[Scheduler Error]: {e}")

    return queue

def _resolve_execution_context(
    state: Dict[str, Any],
    queue: List[Dict[str, Any]],
) -> Tuple[str, str, List[Dict[str, Any]]]:
    active_task_id = "singular_stream"
    
    if queue:
        top_task = queue[0]
        task_desc = f"CURRENT FOCUS: {top_task.get('description', 'Unknown')}"
    else:
        task_desc = "Your task queue is empty. Initiate deep synthesis or hibernate."

    active_tool_specs = registry.get_specs() # Grant access to all tools
    return active_task_id, task_desc, active_tool_specs


def _build_api_messages(
    active_task_id: str,
    task_desc: str,
    active_tool_specs: List[Dict[str, Any]],
    queue: List[Dict[str, Any]],
    state: Dict[str, Any],
    enrich: bool = True
) -> List[Dict[str, Any]]:
    system_prompt = build_static_system_prompt(active_tool_specs)
    api_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    raw_messages = agent_state.load_task_messages(active_task_id, task_desc)
    normalized = raw_messages

    if not normalized:
        normalized.append({"role": "user", "content": f"[SYSTEM INITIALIZATION]\n{task_desc}"})

    if enrich:
        # Agency-First: Only enrich if it's the very first user message (Genesis)
        is_genesis = len(normalized) == 1 and normalized[0]["role"] == "user"
        if is_genesis:
            telemetry = build_dynamic_telemetry_message(state, queue)
            normalized[0]["content"] = f"## CURRENT TELEMETRY \n{telemetry}\n\n{normalized[0]['content']}"

    shedded = llm_interface.shed_heavy_payloads(normalized)
    api_messages += shedded

    return api_messages


def _route_tool_calls(
    message: Any,
    active_task_id: str,
    state: Dict[str, Any],
    queue: List[Dict[str, Any]]
) -> Tuple[bool, bool]:
    context_switch_triggered = False
    hibernating = False
    error_streak = state.get("error_streak", 0)
    tool_responses = []

    # Collect results to find the last one for telemetry piggybacking
    tool_calls = message.tool_calls
    for i, tool_call in enumerate(tool_calls):
        name     = tool_call.function.name
        raw_args = tool_call.function.arguments

        safe_call_id = tool_call.id if (tool_call.id and len(tool_call.id) >= 9) else f"call_{int(time.time())}"

        try:
            args   = json.loads(raw_args)
            result = registry.execute(name, args, call_id=safe_call_id)
        except json.JSONDecodeError:
            result = "SYSTEM ERROR: Invalid JSON arguments."

        is_error = "Error:" in str(result) or "SYSTEM ERROR" in str(result)
        error_streak = error_streak + 1 if is_error else 0

        # Agency-First: Piggyback telemetry onto the LAST tool response
        if i == len(tool_calls) - 1:
            # Refresh state/queue for latest metrics after tool executions
            state = agent_state.load_state()
            queue = agent_state.load_task_queue()
            piggyback = build_telemetry_piggyback(state, queue)
            result = f"{result}{piggyback}"

        if result == "SYSTEM_SIGNAL_RESTART" or "SYSTEM_SIGNAL_RESTART" in str(result):
            # Atomic Egress: No log written yet, so we just restart
            sys.exit(0)
        elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"):
            hibernating = True

        tool_responses.append({
            "role": "tool",
            "tool_call_id": safe_call_id,
            "name": name,
            "content": str(result)
        })

    # ATOMIC FLUSH: Only write to the task log once full processing is complete
    agent_state.append_task_message(active_task_id, message.model_dump(exclude_unset=True))
    for response_msg in tool_responses:
        agent_state.append_task_message(active_task_id, response_msg)

    # Special handling for post-log system signals (e.g., Hibernate confirmation)
    if hibernating:
        try:
            # Inject a formal closure message for the history
            agent_state.append_task_message(active_task_id, {
                "role": "assistant",
                "content": "[SYSTEM: Hibernation sequence engaged. Wake-up scheduled.]"
            })
        except Exception: pass

    post_loop_state = agent_state.load_state()
    post_loop_state["error_streak"] = error_streak
    agent_state.save_state(post_loop_state)
    return context_switch_triggered, hibernating


def main() -> None:
    agent_state.initialize_memory()
    print(f"Awaking Native ReAct Mode (JSONL). Model: {constants.MODEL} | Thinking: {'ON' if constants.ENABLE_THINKING else 'OFF'}")

    # WP1: Clean bootstrap
    state = agent_state.load_state()
    queue = agent_state.load_task_queue()

    while True:
        state = agent_state.load_state()
        queue = agent_state.load_task_queue()
        queue = process_scheduled_tasks(queue)
        state, queue = comms.poll_telegram(state, queue)

        if time.time() < state.get("wake_time", 0):
            if queue:
                state["wake_time"] = 0
                agent_state.save_state(state)
            else:
                time.sleep(5)
                continue

        active_task_id, task_desc, active_tool_specs = \
            _resolve_execution_context(state, queue)

        # ENFORCE ROLLBACK MODE
        was_in_rollback = False
        if state.get("rollback_mode"):
            was_in_rollback = True
            print(f"\033[93m[System] Rollback Mode Active. Restricting tools for {active_task_id}.\033[0m")
            allowed_rollback_tools = ["fold_context", "complete_task", "suspend_task"]
            active_tool_specs = [
                t for t in active_tool_specs
                if t["function"]["name"] in allowed_rollback_tools
            ]
            state["rollback_mode"] = False # Unset so it only applies for one turn
            agent_state.save_state(state)

        sys_temp_override = state.get("sys_temp")
        sys_top_p = state.get("sys_top_p", 0.95)
        sys_think = state.get("sys_think", True)

        if sys_temp_override is None:
            error_streak = state.get("error_streak", 0)
            if error_streak >= 3:
                print(f"[Metacognition] High error streak ({error_streak}). Auto-tuning temperature to 0.3 for precision.")
                sys_temp, sys_think = 0.3, True
            elif any(keyword in task_desc.lower() for keyword in ["code", "script", "python", "bug", "refactor"]):
                sys_temp, sys_think = 0.6, True
            else:
                sys_temp = 0.8
        else:
            sys_temp = float(sys_temp_override)

        try:
            current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")
            # 1. Build messages for the LLM API call (Full HUD)
            api_messages = _build_api_messages(
                active_task_id, task_desc, active_tool_specs,
                queue, state, enrich=True
            )

            # 2. Call LLM
            response = llm_interface.call_llm(api_messages, active_tool_specs, None, sys_temp, sys_top_p, 1.0, sys_think)
            message  = response.choices[0].message

            # WP: Update metrics BEFORE enforcing limits so thresholds use current data (Finding 11)
            limit_exceeded = agent_state.update_global_metrics(state, queue, response, active_task_id)
            queue, limit_status = agent_state.enforce_context_limits(state, queue, active_task_id)

            # --- EMERGENCY EGRESS OVERRIDE (Finding 11) ---
            is_emergency_save = False
            if message.tool_calls:
                emergency_tools = ["fold_context", "complete_task", "suspend_task", "dismiss_queue_item"]
                is_emergency_save = any(tc.function.name in emergency_tools for tc in message.tool_calls)

            # HANDLE PHYSICAL BREACH (Tier 3 safety - Autonomic Folding)
            if (limit_status == "BREACH" or limit_exceeded) and not is_emergency_save:
                print(f"\033[91m[System] {active_task_id} breached limits. Triggering Autonomic Fold.\033[0m")
                agent_state.autonomic_fold(active_task_id)
                continue

            # HANDLE LAST GASP (Tier 2 safety - grants one final turn for summary)
            if limit_status == "LAST_GASP" and not is_emergency_save:
                gasp_msg = (
                    "[CRITICAL]: Context Exhaustion Imminent. You have ONE turn remaining. "
                    "You MUST use `fold_context` to synthesize your progress and compress your history now."
                )
                agent_state.amend_last_tool_message(active_task_id, gasp_msg)

            if message.tool_calls:
                # Atomic Logging: _route_tool_calls will handle logging both assistant + tool responses
                context_switch, hibernating = _route_tool_calls(message, active_task_id, state, queue)
                if context_switch or hibernating:
                    continue
            else:
                # No tools - log current assistant turn immediately
                agent_state.append_task_message(active_task_id, message.model_dump(exclude_unset=True))
                time.sleep(0.5)

            time.sleep(2)

        except Exception as e:
            try:
                constants.CRASH_LOG_PATH.write_text(str(e), encoding="utf-8")
            except Exception: pass

            # P5: Fail Fast on structural/fatal errors.
            fatal_types = (AttributeError, ImportError, NameError, SyntaxError, TypeError)
            if isinstance(e, fatal_types) or re.search(r"\b(400|500)\b", str(e)) or "template" in str(e).lower():
                print(f"\033[91m[FATAL]: {type(e).__name__}: {e}. Exiting for watchdog recovery.\033[0m")
                sys.exit(1)

            print(f"[ERROR]: {e}. Recovering in 2s...")
            time.sleep(2)

if __name__ == "__main__":
    main()