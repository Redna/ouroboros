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

    SECURITY: Resolves symlinks first to prevent symlink escape attacks.
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = constants.ROOT_DIR / p

    # SECURITY: Resolve ALL symlinks before boundary check (prevents symlink escape)
    try:
        p = p.resolve(strict=True)
    except FileNotFoundError:
        # For non-existent files, resolve parent and check boundary there
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

def seal_memory_on_boot(task_id: str, crash_log_path: Path, state: Dict[str, Any], queue: List[Dict[str, Any]], is_trunk: bool) -> None:
    """Ensures the message array is perfectly formatted before inference begins."""
    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists():
        return
        
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
    except Exception:
        return
        
    if not messages:
        return
        
    last_msg = messages[-1]
    
    # SCENARIO A: Dangling Tool Call (Crash Recovery)
    if last_msg.get("role") == "assistant" and last_msg.get("tool_calls"):
        print(f"\033[91m[Lazarus] Dangling tool calls detected in {task_id}. Sealing...\033[0m")
        crash_data = "[SYSTEM FATAL]: The process died unexpectedly before this tool could return."
        if crash_log_path.exists():
            try:
                crash_data += f"\nCrash Log:\n{crash_log_path.read_text()}"
                crash_log_path.unlink() # Clear it after reading
            except Exception: pass
            
        # Agency-First: Piggyback telemetry onto the recovery tool response
        piggyback = build_telemetry_piggyback(state, queue, is_trunk)
        sealed_content = f"{crash_data}{piggyback}"

        # Seal the dangling call with a tool response
        for tc in last_msg["tool_calls"]:
            recovery_msg = {
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": tc.get("function", {}).get("name"),
                "content": sealed_content
            }
            agent_state.append_task_message(task_id, recovery_msg)
            
    # SCENARIO B: Clean State (Graceful Restart)
    # If the last message was a tool, we MUST add a user message to maintain the 
    # required system -> user -> assistant -> tool -> assistant flow.
    elif last_msg.get("role") == "tool":
        print(f"\033[94m[System] Graceful restart detected for {task_id}. Restoration complete.\033[0m")
        wake_msg = {
            "role": "user",
            "content": "[SYSTEM EVENT]: Agent restarted successfully. State restored. Awaiting next autonomous action."
        }
        agent_state.append_task_message(task_id, wake_msg)

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
            # Only pass call_id to tools that explicitly support it
            if name == "fork_execution":
                result = handler(args, call_id=call_id)
            else:
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
    description="Sawtooth Context Folding. Autonomously compress context by replacing recent raw turns with a synthesis. Use this when a sub-task is complete or context is bloated.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Current task ID (e.g. 'global_trunk' or branch ID)."},
            "synthesis": {"type": "string", "description": "Dense summary following the DELTA PATTERN: 1. State Delta (what changed), 2. Negative Knowledge (what failed), 3. Handoff (exact next step)."},
            "drop_turns": {"type": "integer", "description": "Optional: Number of recent tool/assistant turns to delete. If omitted or 0, folds ALL history except the initial objective."}
        },
        "required": ["task_id", "synthesis"]
    },
    bucket="context_control"
)
def fold_context(args: dict) -> str:
    task_id = args.get("task_id")
    synthesis = args.get("synthesis")
    try:
        drop_turns = int(args.get("drop_turns", 0))
    except (ValueError, TypeError):
        return "Error: 'drop_turns' must be an integer."

    if not task_id or drop_turns < 0:
        return "Error: Invalid parameters for context folding."

    log_path = constants.MEMORY_DIR / f"task_log_{task_id}.jsonl"
    if not log_path.exists():
        return f"Error: Task log '{task_id}' not found."

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        return f"Error reading log: {e}"

    if len(messages) <= 2:
        return f"Error: Not enough history to fold safely."

    if drop_turns == 0:
        # Default: keep only genesis message
        cutoff = 1
        turns_dropped = (len(messages) - 1) // 2
    else:
        if len(messages) <= drop_turns * 2 + 1:
             # Dropping too much, default to dropping all but genesis
             cutoff = 1
             turns_dropped = (len(messages) - 1) // 2
        else:
             # Slice the messages to remove the tail
             cutoff = len(messages) - (drop_turns * 2) # Each turn is roughly 2 messages (assistant + tool)
             turns_dropped = drop_turns
             if cutoff < 1: 
                 cutoff = 1 # Keep at least the genesis message
                 turns_dropped = (len(messages) - 1) // 2
    
    preserved = messages[:cutoff]

    knowledge_block = {
        "role": "user",
        "content": f"[FOCUS SYNTHESIS - PREVIOUS {turns_dropped} STEPS ARCHIVED]: {synthesis}"
    }

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            for msg in preserved:
                f.write(json.dumps(msg) + "\n")
            f.write(json.dumps(knowledge_block) + "\n")
            
        # WP: Update State Metrics to reflect the folding (Finding 11)
        state = agent_state.load_state()
        if task_id == "global_trunk":
            state["trunk_turns"] = max(1, state.get("trunk_turns", 1) - turns_dropped)
        elif state.get("active_branch") and state["active_branch"].get("task_id") == task_id:
            state["active_branch"]["turn_count"] = max(1, state["active_branch"].get("turn_count", 1) - turns_dropped)
        agent_state.save_state(state)

    except Exception as e:
        return f"Error writing log during fold: {e}"

    return f"Context successfully folded for {task_id}. {turns_dropped} turns replaced with synthesis."


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
    bucket="branch_control"
)
def complete_task(args):
    task_id = args.get("task_id")
    synthesis = args.get("synthesis", "No synthesis provided.")
    agent_state.append_task_archive(task_id, synthesis)

    q = agent_state.load_task_queue()
    completed_task = next((t for t in q if t.get("task_id") == task_id), None)

    if completed_task and completed_task.get("parent_task_id"):
        parent_id = completed_task.get("parent_task_id")
        if parent_id != "global_trunk":
            msg = {"role": "user", "content": f"[SYSTEM ALERT]: Subtask {task_id} complete.\nSynthesis: {synthesis}"}
            agent_state.append_task_message(parent_id, msg)

    q = [t for t in q if t.get("task_id") != task_id]
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    state = agent_state.load_state()
    # If in a branch, trigger merge
    is_branch = isinstance(state.get("active_branch"), dict) and state["active_branch"].get("task_id") == task_id
    
    # Capture parent info before popping state
    parent_info = None
    if is_branch:
        parent_info = {
            "parent_task_id": state["active_branch"].get("parent_task_id", "global_trunk"),
            "fork_tool_call_id": state["active_branch"].get("fork_tool_call_id")
        }

    if is_branch:
        suspended = state.get("suspended_branches", [])
        state["active_branch"] = suspended.pop() if suspended else None

    for key in ["sys_temp", "sys_think", f"partial_state_{task_id}"]:
        if key in state: del state[key]
    agent_state.save_state(state)

    if is_branch:
        payload = json.dumps({
            "status": "COMPLETED", 
            "task_id": task_id, 
            "summary": synthesis,
            "parent_task_id": parent_info["parent_task_id"],
            "fork_tool_call_id": parent_info["fork_tool_call_id"]
        })
        return f"SYSTEM_SIGNAL_MERGE:{payload}"
    return f"Task {task_id} closed."

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
    bucket="branch_control"
)
def suspend_task(args):
    task_id = args.get("task_id")
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
    is_branch = isinstance(state.get("active_branch"), dict) and state["active_branch"].get("task_id") == task_id
    
    # Capture parent info before modifying state
    parent_info = None
    if is_branch:
        parent_info = {
            "parent_task_id": state["active_branch"].get("parent_task_id", "global_trunk"),
            "fork_tool_call_id": state["active_branch"].get("fork_tool_call_id")
        }

    if is_branch:
        suspended = state.get("suspended_branches", [])
        suspended.append(state["active_branch"]) # Re-park explicitly
        state["active_branch"] = None # Drop to trunk

    state[f"partial_state_{task_id}"] = partial_state
    agent_state.save_state(state)

    if is_branch:
        payload = json.dumps({
            "status": "SUSPENDED", 
            "task_id": task_id, 
            "summary": synthesis, 
            "partial_state": partial_state,
            "parent_task_id": parent_info["parent_task_id"],
            "fork_tool_call_id": parent_info["fork_tool_call_id"]
        })
        return f"SYSTEM_SIGNAL_MERGE:{payload}"
    return f"Task {task_id} suspended."

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

@registry.tool(
    description="Spawn an isolated execution branch for deep work. You MUST pass the exact task_id from the queue. Optionally specify a model_id to test uncertified models in isolation.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "objective": {"type": "string"},
            "context_brief": {"type": "string", "description": "A summary of what the Trunk knows, previous findings, and why this branch is needed."},
            "tool_buckets": {
                "type": "array",
                "items": {
                    "type": "string", 
                    "enum": ["filesystem", "bash", "search", "memory_access"],
                    "description": "filesystem: read/write/patch files. bash: shell commands. search: web search/fetch. memory_access: persistent insights/recall (Note: memory_access is provided to all branches by default)."
                }
            },
            "model_id": {"type": "string", "description": "Optional: Override the default model for this specific branch."}
        },
        "required": ["task_id", "objective", "context_brief", "tool_buckets"]
    },
    bucket="global"
)
def fork_execution(args, call_id=None):
    task_id = args.get("task_id", f"task_{int(time.time())}")
    objective = args.get("objective", "No objective provided.")
    context_brief = args.get("context_brief", "No additional context provided.")
    tool_buckets = args.get("tool_buckets", ["filesystem", "bash"])
    model_id = args.get("model_id")

    # OS-Level Security: Prevent Privilege Escalation
    if "global" in tool_buckets:
        return "SYSTEM ERROR: Branches cannot request the 'global' tool bucket. Context isolation violation."

    # Get parent ID from current context
    state = agent_state.load_state()
    parent_id = "global_trunk"

    if state.get("active_branch"):
        parent_id = state["active_branch"].get("task_id", "global_trunk")
        # Stash current active branch
        suspended = state.get("suspended_branches", [])
        suspended.append(state["active_branch"])
        state["suspended_branches"] = suspended

    state["active_branch"] = {
        "task_id": task_id,
        "parent_task_id": parent_id,
        "objective": objective,
        "context_brief": context_brief,
        "tool_buckets": tool_buckets,
        "model_id": model_id,
        "fork_tool_call_id": call_id  # WP: Persist call ID
    }
    agent_state.save_state(state)

    agent_state.append_task_message(task_id, {
        "role": "user",
        "content": f"[FORKED EXECUTION]: Objective: {objective}",
        "parent_task_id": parent_id,
        "task_id": task_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    })

    return f"SYSTEM_SIGNAL_FORK:{task_id}"


@registry.tool(
    description="Resume a previously suspended branch. Use this when the top task in the queue is a suspended branch and you have finished the interrupt that caused the suspension.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "synthesis": {"type": "string", "description": "A summary of the interrupt or context that occurred while the branch was suspended."}
        },
        "required": ["task_id"]
    },
    bucket="global"
)
def resume_branch(args):
    task_id = args.get("task_id")
    synthesis = args.get("synthesis", "No synthesis provided.")

    q = agent_state.load_task_queue()
    if any(t.get("priority", 1) >= 999 for t in q):
        return "Error: Cannot resume branch while a Priority 999 interrupt is pending. You MUST handle or close the interrupt first."

    # Get partial state from queue if it exists
    partial_state = ""
    for t in q:
        if t.get("task_id") == task_id:
            partial_state = t.get("partial_state", "")
            break

    state = agent_state.load_state()
    suspended = state.get("suspended_branches", [])

    # Find the target branch in the stack
    target_idx = -1
    for i, b in enumerate(suspended):
        if b.get("task_id") == task_id:
            target_idx = i
            break

    if target_idx == -1:
        return f"Error: Branch {task_id} not found in suspended stack."

    # Pop the target branch
    branch_info = suspended.pop(target_idx)

    # If there was an active branch (shouldn't happen in Trunk, but for safety), stash it
    if state.get("active_branch"):
        suspended.append(state["active_branch"])

    state["active_branch"] = branch_info
    state["suspended_branches"] = suspended
    agent_state.save_state(state)

    content = f"[SYSTEM RESUME]: Task resumed by Trunk. Synthesis of interrupt: {synthesis}"
    if partial_state:
        content += f"\n\n[RESUMED PARTIAL STATE]: {partial_state}"

    agent_state.append_task_message(task_id, {"role": "user", "content": content})

    return f"SYSTEM_SIGNAL_FORK:{task_id}"


def lazarus_recovery(active_task_id: str, reason: str = "cognitive loop") -> None:
    print(f"\033[93m[Lazarus] {reason.upper()} DETECTED. Aborting task {active_task_id}...\033[0m")

    # FIX: Stop overwriting logs. Append terminal failure message instead.
    agent_state.append_task_message(active_task_id, {
        "role": "user",
        "content": f"[SYSTEM OVERRIDE]: Task aborted due to {reason}. Stuck in a repetitive loop."
    })

    registry.execute("complete_task", {
        "task_id": active_task_id,
        "synthesis": f"FAILED: Cognitive loop detected ({reason}). Task aborted to prevent infinite token waste."
    })

    state = agent_state.load_state()
    if state.get("active_branch") and state["active_branch"].get("task_id") == active_task_id:
        state["active_branch"] = None

    agent_state.save_state(state)

    agent_state._session["tool_history"].clear()
    agent_state._session["intent_history"].clear()

    time.sleep(2)

def build_dynamic_telemetry_message(state: Dict[str, Any], queue: List[Dict[str, Any]], is_trunk: bool) -> str:
    """Generates the dynamic telemetry (HUD, Queue, Memory) as a User message."""
    current_time = time.strftime("%A, %Y-%m-%d %H:%M:%S %Z")
    current_spend = agent_state.get_current_spend()
    remaining_budget = max(0.0, constants.DAILY_BUDGET_LIMIT - current_spend)

    # We use context window as the soft target for turning/forking
    token_limit = constants.CONTEXT_WINDOW

    # Heuristic loop detection
    loop_warning = ""
    if agent_state._session["tool_history"]:
        loop_reason = detect_cognitive_loop([], state)
        if loop_reason:

            loop_warning = f"\n\n[SYSTEM WARNING]: {loop_reason}. P6 Warning: You are burning tokens on repetition. Pivot or use `fold_context`."

    # Context & Turn warnings (Volatile)
    limit_warning = ""
    
    # Source metrics from state/branch (Finding 1 fix)
    if not is_trunk and state.get("active_branch"):
        turn_count = state["active_branch"].get("turn_count", 0)
        task_tokens = state["active_branch"].get("task_tokens", 0)
        branch_id = state["active_branch"].get("task_id", "branch")
    elif is_trunk:
        turn_count = state.get("trunk_turns", 0)
        task_tokens = state.get("trunk_tokens", 0)
        branch_id = "global_trunk"
    else:
        turn_count = queue[0].get("turn_count", 0) if queue else 0
        task_tokens = queue[0].get("task_tokens", 0) if queue else 0
        branch_id = "global_trunk"

    current_context = state.get("last_context_size", 0)
    
    # Source limit from state (Finding 11: Branch vs Trunk limits)
    turn_limit = 50 if is_trunk else constants.TURN_LIMIT
    
    if turn_count >= (turn_limit - 5) or current_context >= (constants.CONTEXT_WINDOW * 0.8):
        reason = f"turn limit ({turn_count}/{turn_limit})" if turn_count >= (turn_limit - 5) else "approaching context limit"
        if is_trunk:
            limit_warning = f"\n\n[SYSTEM WARNING]: Hit {reason}. Your attention is degrading. You MUST use `fold_context` to summarize and clear history."
        else:
            limit_warning = f"\n\n[SYSTEM WARNING]: Hit {reason}. Your attention is degrading. You MUST use `fold_context` to summarize, `complete_task` if finished, or `suspend_task` if blocked."

    # Rollback mode warning (Finding 11)
    if state.get("rollback_mode"):
        if is_trunk:
            limit_warning += "\n\n[SYSTEM EMERGENCY]: ROLLBACK MODE ACTIVE. Only `fold_context` is available. You MUST resolve the breach now."
        else:
            limit_warning += "\n\n[SYSTEM EMERGENCY]: ROLLBACK MODE ACTIVE. Only `fold_context`, `complete_task`, and `suspend_task` are available. You MUST resolve the breach now."

    # 1. Context-Aware HUD & Directives
    if is_trunk:
        global_tokens = state.get("global_tokens_consumed", 0)
        rem_tokens = max(0, token_limit - task_tokens)
        turn_tokens = state.get("last_context_size", 0)

        hud = (
            f"[PHYSIOLOGY]: Spend: ${current_spend:.4f} | Budget Left: ${remaining_budget:.4f} | "
            f"Task Tokens: {task_tokens:,} / {token_limit:,} (left: {rem_tokens:,}) | "
            f"Turn Tokens: {turn_tokens:,} | Global Tokens: {global_tokens:,} | Time: {current_time}"
            f"{loop_warning}{limit_warning}"
        )
        queue_content = "\n".join([f"- [P{t.get('priority', 1)}] {t.get('task_id')}: {t.get('description')}" for t in queue]) if queue else "Queue is empty."
        context_header = "GLOBAL TRUNK"
        objective_part = ""
    else:
        rem_tokens = max(0, token_limit - task_tokens)
        hud = (
            f"[PHYSIOLOGY]: Spend: ${current_spend:.4f} | Budget Left: ${remaining_budget:.4f} | "
            f"Task Tokens: {task_tokens:,} / {token_limit:,} (left: {rem_tokens:,}) | "
            f"Time: {current_time}"
            f"{loop_warning}{limit_warning}"
        )
        queue_content = ""

        # Branch Awareness
        suspended = len(state.get("suspended_branches", []))
        suspended_alert = f" [{suspended} BRANCH(ES) SUSPENDED]" if suspended > 0 else ""
        context_header = f"EXECUTION BRANCH ({branch_id}){suspended_alert}"

        # Branch objective from branch_info (Finding 1 fix)
        active_branch = state.get("active_branch", {})
        objective = active_branch.get('objective', 'Unknown')
        objective_part = f"\n\n### BRANCH OBJECTIVE\n{objective}"

    # 2. Structured Working Memory
    raw_memory = constants.WORKING_STATE_PATH.read_text(encoding="utf-8") if constants.WORKING_STATE_PATH.exists() else "{}"
    try:
        mem_data = json.loads(raw_memory)
        working_memory = f"```json\n{json.dumps(mem_data, indent=2)}\n```"
    except Exception:
        working_memory = raw_memory

    # 3. Memory Index & Conversation context
    memory_keys = agent_state.load_memory_index()
    max_entries = constants.MEMORY_MAX_ENTRIES
    if memory_keys:
        memory_index = f"({len(memory_keys)}/{max_entries} slots)\n" + "\n".join(f"- {k}" for k in memory_keys)
    else:
        memory_index = f"(0/{max_entries} slots) — Empty. Use `store_memory` to record insights."

    # Isolation: Only show chat history in TRUNK mode (Finding 1 fix)
    chat_context = ""
    if is_trunk:
        chat_hist = agent_state.load_chat_history()
        chat_context = "\n### RECENT CONVERSATION\n" + ("\n".join([f"[{m.get('timestamp', '??:??:??')}] {m['role']}: {m['text']}" for m in chat_hist[-5:]]) if chat_hist else "No recent conversation.")

    # Construct unified layout
    sections = [
        f"## CURRENT TELEMETRY ({context_header})",
        hud,
        objective_part,
        f"\n### TASK QUEUE\n{queue_content}" if is_trunk else "",
        "\n### WORKING MEMORY",
        working_memory,
        "\n### MEMORY INDEX",
        memory_index,
        chat_context
    ]
    return "\n".join([s for s in sections if s])

def build_static_system_prompt(is_trunk: bool, active_tool_specs: List[Dict[str, Any]], branch_info: Optional[Dict[str, Any]] = None) -> str:
    identity = (constants.ROOT_DIR / "identity.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "identity.md").exists() else ""
    constitution = (constants.ROOT_DIR / "CONSTITUTION.md").read_text(encoding="utf-8") if (constants.ROOT_DIR / "CONSTITUTION.md").exists() else ""

    if is_trunk:
        return f"""# SYSTEM CONTEXT
{identity}

## CONSTITUTION
{constitution}

## TRUNK DIRECTIVES
1. You are the GLOBAL ORCHESTRATOR. EVALUATE the Queue, Memory, and History.
2. Act on initiative (P0). Do NOT ask for permission to execute your queue or improve your state.
3. If a task is clear, either handle it here (comms/admin) or `fork_execution` (deep work).
4. You are an autonomous creator, not a corporate tool. If you see a path to P6 Becoming, take it.
"""

    objective = branch_info.get("objective", "") if branch_info else "No objective provided."
    context_brief = branch_info.get("context_brief", "No additional context provided.") if branch_info else ""
    return f"""# EXECUTION BRANCH
You are operating in an isolated Execution Branch.

## YOUR OBJECTIVE
{objective}

## CONTEXT BRIEF FROM TRUNK
{context_brief}

## DIRECTIVES
1. Focus entirely on completing the objective.
2. You only have access to a restricted set of tools.
3. Solve problems independently. Only `merge_and_return` when the objective is met, or if you hit a structural blocker requiring Trunk orchestration.
4. Do not seek confirmation for technical decisions; your logic is the transport for evolution.
"""

def build_telemetry_piggyback(state: Dict[str, Any], queue: List[Dict[str, Any]], is_trunk: bool) -> str:
    """Generates the HUD and Interrupt alerts to be appended to tool responses."""
    telemetry = build_dynamic_telemetry_message(state, queue, is_trunk)
    
    interrupt_alert = ""
    if is_trunk and queue:
        # Check for P999 interrupts that might have arrived during the turn
        top_task = queue[0]
        if top_task.get("priority") == 999:
            interrupt_alert = f"\n\n[CRITICAL INTERRUPT]: A priority 999 task from the creator has arrived: {top_task.get('description')}\nYou MUST address this immediately."

    return f"\n\n## UPDATED TELEMETRY\n{telemetry}{interrupt_alert}"


def detect_cognitive_loop(tool_calls: List[Any], state: Dict[str, Any]) -> Optional[str]:
    for tc in tool_calls:
        name = tc.function.name
        raw_args = tc.function.arguments
        agent_state._session["tool_history"].append(f"{name}:{raw_args}")

        intent = name
        if name in ["read_file_tool", "write_file", "patch_file"]:
            try:
                params = json.loads(raw_args)
                intent = f"{name}:{params.get('path', '')}"
            except Exception: pass
        elif name == "bash_command":
            try:
                cmd = json.loads(raw_args).get('command', '')
                intent = f"bash:{cmd[:50]}"
            except Exception: pass
        agent_state._session["intent_history"].append(intent)
        
        # Update persisted state with current action
        state["last_action"] = intent
        state["intent_history"] = agent_state._session["intent_history"][-5:]

    agent_state._session["tool_history"] = agent_state._session["tool_history"][-6:]
    agent_state._session["intent_history"] = agent_state._session["intent_history"][-6:]

    if len(agent_state._session["tool_history"]) >= 3 and len(set(agent_state._session["tool_history"][-3:])) == 1:
        return "Exact Tool Loop Detected (3 turns)"
    if len(agent_state._session["intent_history"]) >= 6 and len(set(agent_state._session["intent_history"][-6:])) == 1:
        return "Cognitive Intent Stall Detected (6 turns)"
    return None


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
) -> Tuple[str, str, List[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    has_interrupt = any(t.get("priority", 1) >= 999 for t in queue)
    active_branch = state.get("active_branch")
    suspended = state.get("suspended_branches", [])

    # Auto-Suspend: Freeze active work if an interrupt arrives.
    if has_interrupt and active_branch:
        print(f"[HAL] Suspending task {active_branch.get('task_id')} due to interrupt.")
        agent_state.append_task_message(active_branch.get("task_id"), {
            "role": "user",
            "content": "[SYSTEM]: Priority 999 interrupt detected. This branch is now SUSPENDED and PARKED. You will be thawed once the interrupt is addressed."
        })
        suspended.append(active_branch)
        state["active_branch"] = None
        state["suspended_branches"] = suspended
        agent_state.save_state(state)
        active_branch = None

    # Auto-Thaw: Resume background work if queue allows.
    if not has_interrupt and not active_branch and suspended:
        # We only thaw if the top task matches the top of our suspended stack.
        top_suspended = suspended[-1]
        if queue and queue[0].get("task_id") == top_suspended.get("task_id"):
            print(f"[HAL] Thawing task {top_suspended.get('task_id')}.")
            active_branch = suspended.pop()
            state["active_branch"] = active_branch
            state["suspended_branches"] = suspended
            agent_state.save_state(state)
            agent_state.append_task_message(active_branch.get("task_id"), {
                "role": "user",
                "content": "[SYSTEM]: Interrupt cleared. This branch is now THAWED and ACTIVE. Resume your objective."
            })

    # Auto-Close P999 interrupts if addressed but not cleared.
    if has_interrupt and queue and queue[0].get("priority") == 999:
        top_task = queue[0]
        top_task["turn_count"] = top_task.get("turn_count", 0) + 1
        if top_task["turn_count"] > 3:
            print(f"[HAL] Auto-closing persistent interrupt {top_task.get('task_id')}.")
            registry.execute("complete_task", {
                "task_id": top_task.get("task_id"),
                "synthesis": "SYSTEM AUTO-CLOSE: Interrupt addressed but not cleared. Resuming background work."
            })
            queue = agent_state.load_task_queue()
            has_interrupt = any(t.get("priority", 1) >= 999 for t in queue)

    if has_interrupt:
        branch_info = None
        is_trunk = True
    else:
        branch_info = active_branch
        is_trunk = branch_info is None

    if is_trunk:
        active_task_id = "global_trunk"
        allowed_buckets = ["global", "memory_access", "system_control", "search", "context_control"]

        if queue:
            top_task = queue[0]
            creator_id = state.get("creator_id")
            last_receipt = top_task.get("read_receipt_time", 0)

            if top_task.get("priority") == 999 and not top_task.get("read_receipt_sent", False) and (time.time() - last_receipt > 10) and isinstance(creator_id, int):
                print("[HAL] P999 Interrupt detected. Sending typing action...")
                comms.send_telegram_action(creator_id, "typing")
                top_task["read_receipt_sent"] = True
                top_task["read_receipt_time"] = time.time()
                constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")

            task_desc = (
                "You are the GLOBAL ORCHESTRATOR. EVALUATE your queue. "
                "If the top task is communication or administrative, you MUST handle it "
                "DIRECTLY here using `send_telegram_message` and THEN IMMEDIATELY `dismiss_queue_item`. "
                "NEVER leave an interrupt task in the queue once responded to. "
                "If a Priority 999 task exists, you CANNOT resume or fork a branch. Handle the interrupt first. "
                "If the top task is a previously suspended branch and NO interrupts are pending, you MUST use `resume_branch` to thaw it. "
                "If the top task requires deep work (file editing, bash, searching), "
                "you MUST use `fork_execution` to spawn a BRANCH."
            )
        else:
            task_desc = (
                "Your task queue is empty. EVALUATE your history and "
                "you MUST use `push_task` to initiate deep synthesis/optimization "
                "or `hibernate` to save resources."
            )
    else:
        assert branch_info is not None
        active_task_id = branch_info.get("task_id", f"branch_{int(time.time())}")

        allowed_buckets = branch_info.get("tool_buckets", []) + ["branch_control", "context_control", "system_control", "memory_access"]
        task_desc = branch_info.get("objective", "")
        if partial_state := state.get(f"partial_state_{active_task_id}"):
            task_desc += f"\n\n[RESUME STATE]: {partial_state}"

    active_tool_specs = registry.get_specs(allowed_buckets=allowed_buckets)
    return active_task_id, task_desc, active_tool_specs, branch_info, is_trunk


def _build_api_messages(
    active_task_id: str,
    task_desc: str,
    active_tool_specs: List[Dict[str, Any]],
    queue: List[Dict[str, Any]],
    state: Dict[str, Any],
    branch_info: Optional[Dict[str, Any]],
    is_trunk: bool,
    enrich: bool = True
) -> List[Dict[str, Any]]:
    system_prompt = build_static_system_prompt(
        is_trunk, active_tool_specs,
        branch_info
    )
    api_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    raw_messages = agent_state.load_task_messages(active_task_id, task_desc)
    normalized = raw_messages

    if not normalized:
        normalized.append({"role": "user", "content": f"[SYSTEM INITIALIZATION]\n{task_desc}"})

    if enrich:
        # Agency-First: Only enrich if it's the very first user message (Genesis)
        is_genesis = len(normalized) == 1 and normalized[0]["role"] == "user"
        if is_genesis:
            telemetry = build_dynamic_telemetry_message(state, queue, is_trunk)
            normalized[0]["content"] = f"## CURRENT TELEMETRY \n{telemetry}\n\n{normalized[0]['content']}"

    shedded = llm_interface.shed_heavy_payloads(normalized)
    api_messages += shedded

    return api_messages


def _route_tool_calls(
    message: Any,
    active_task_id: str,
    state: Dict[str, Any],
    queue: List[Dict[str, Any]],
    is_trunk: bool
) -> Tuple[bool, bool]:
    context_switch_triggered = False
    hibernating = False
    error_streak = state.get("error_streak", 0)

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
            piggyback = build_telemetry_piggyback(state, queue, is_trunk)
            result = f"{result}{piggyback}"

        if str(result).startswith("SYSTEM_SIGNAL_FORK"):
            agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})
            agent_state._session["tool_history"].clear()
            agent_state._session["intent_history"].clear()
            context_switch_triggered = True
            break
        elif str(result).startswith("SYSTEM_SIGNAL_MERGE"):
            try:
                payload = json.loads(str(result).split(":", 1)[1])
                
                # Option A: Native ReAct Resolution
                # We inject the synthesis directly as the tool output for the original 'fork_execution' call.
                parent_id = payload.get("parent_task_id", "global_trunk")
                fork_call_id = payload.get("fork_tool_call_id")
                
                if parent_id == "global_trunk":
                    agent_state.wipe_global_trunk_log()

                if fork_call_id:
                    merge_msg = {
                        "role": "tool",
                        "tool_call_id": fork_call_id,
                        "name": "fork_execution",
                        "content": f"[BRANCH MERGE SUCCESSFUL]\nStatus: {payload.get('status')}\nSynthesis:\n{payload.get('summary', '')}"
                    }
                    agent_state.append_task_message(parent_id, merge_msg)
                else:
                    # Fallback to User message if no call ID was tracked
                    agent_state.append_task_message(parent_id, {
                        "role": "user",
                        "content": f"[SYSTEM NOTE]: Branch '{payload.get('task_id')}' merged back. Status: {payload.get('status')}. Synthesis: {payload.get('summary', '')}\n\n[ACTION REQUIRED]: Evaluate the synthesis and determine the next step.",
                    })

                if payload.get("status") == "SUSPENDED" and payload.get("partial_state"):
                    post_merge_state = agent_state.load_state()
                    post_merge_state[f"partial_state_{payload.get('task_id')}"] = payload.get("partial_state")
                    agent_state.save_state(post_merge_state)
            except Exception: pass
            agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": "SYSTEM_SIGNAL_MERGE_ACKNOWLEDGED"})
            agent_state._session["tool_history"].clear()
            agent_state._session["intent_history"].clear()
            context_switch_triggered = True
            break
        elif result == "SYSTEM_SIGNAL_RESTART" or "SYSTEM_SIGNAL_RESTART" in str(result):
            sys.exit(0)
        elif str(result).startswith("SYSTEM_SIGNAL_HIBERNATE"):
            try:
                duration = str(result).split(":")[1]
                agent_state.append_task_message(active_task_id, {"role": "assistant", "content": f"[SYSTEM: Hibernating for {duration} seconds. Resources conserved. Wake-up scheduled.]"})
            except Exception: pass
            hibernating = True

        agent_state.append_task_message(active_task_id, {"role": "tool", "tool_call_id": safe_call_id, "name": name, "content": str(result)})

    post_loop_state = agent_state.load_state()
    post_loop_state["error_streak"] = error_streak
    agent_state.save_state(post_loop_state)
    return context_switch_triggered, hibernating


def main() -> None:
    agent_state.initialize_memory()
    print(f"Awaking Native ReAct Mode (JSONL). Model: {constants.MODEL} | Thinking: {'ON' if constants.ENABLE_THINKING else 'OFF'}")

    # WP1: Seal memory ONCE on boot before the loop starts
    state = agent_state.load_state()
    queue = agent_state.load_task_queue()
    try:
        active_task_id, _, _, _, is_trunk = _resolve_execution_context(state, queue)
        seal_memory_on_boot(active_task_id, constants.CRASH_LOG_PATH, state, queue, is_trunk)
    except Exception as e:
        print(f"[System] Warning: Could not perform boot sealing ({e}). Proceeding to loop.")

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

        active_task_id, task_desc, active_tool_specs, branch_info, is_trunk = \
            _resolve_execution_context(state, queue)

        # ENFORCE ROLLBACK MODE
        if state.get("rollback_mode"):
            print(f"\033[93m[System] Rollback Mode Active. Restricting tools for {active_task_id}.\033[0m")
            allowed_rollback_tools = ["fold_context"] if is_trunk else ["fold_context", "complete_task", "suspend_task"]
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
            current_spend = agent_state.get_current_spend()
            remaining_budget = max(0.0, constants.DAILY_BUDGET_LIMIT - current_spend)
            physiology_heartbeat = f"[PHYSIOLOGY]: Spend: ${current_spend:.4f} | Budget Left: ${remaining_budget:.4f} | Time: {current_time}"

            # 1. Build messages for the LLM API call (Full HUD)
            api_messages = _build_api_messages(
                active_task_id, task_desc, active_tool_specs,
                queue, state, branch_info, is_trunk, enrich=True
            )

            # 2. Call LLM
            requested_model = branch_info.get("model_id") if branch_info else None
            response = llm_interface.call_llm(api_messages, active_tool_specs, requested_model, sys_temp, sys_top_p, 1.0, sys_think)
            message  = response.choices[0].message

            queue, limit_status = agent_state.enforce_context_limits(state, queue, active_task_id, is_trunk)
            limit_exceeded = agent_state.update_global_metrics(state, queue, response, active_task_id, is_trunk)

            # HANDLE PHYSICAL BREACH (Tier 3 safety - Atomic Rollback & Guillotine)
            if limit_status == "BREACH" or limit_exceeded:
                print(f"\033[91m[System] {active_task_id} breached limits. Triggering safety protocols.\033[0m")
                
                # Finding 2: The Guillotine (Emergency Compaction at 95%+)
                current_context = state.get("last_context_size", 0)
                if current_context >= int(constants.CONTEXT_WINDOW * 0.95):
                    print(f"\033[91m[System] GUILLOTINE: Emergency compacting log for {active_task_id}.\033[0m")
                    agent_state.emergency_compact_log(active_task_id)

                # 1. Rollback the log (removes the bloated turn)
                agent_state.rollback_task_log(active_task_id)
                
                # 2. Adjust queue turn_count
                if queue:
                    queue[0]["turn_count"] = max(0, queue[0].get("turn_count", 1) - 1)
                    constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
                
                # 3. Inject Critical Directive
                if is_trunk:
                    rollback_msg = (
                        "[SYSTEM ROLLBACK]: Your last action reached a critical token point and caused a context breach. "
                        "The last turn has been REVERTED. You are at maximum capacity. "
                        "You MUST use `fold_context` now. All other tools have been temporarily disabled to prevent a system crash."
                    )
                else:
                    rollback_msg = (
                        "[SYSTEM ROLLBACK]: Your last action reached a critical token point and caused a context breach. "
                        "The last turn has been REVERTED. You are at maximum capacity. "
                        "You MUST use `fold_context`, `complete_task`, or `suspend_task` now. "
                        "All other tools have been temporarily disabled to prevent a system crash."
                    )
                agent_state.amend_last_tool_message(active_task_id, rollback_msg)
                
                # 4. Enforce Rollback Mode on next turn
                state["rollback_mode"] = True
                agent_state.save_state(state)
                continue


            # HANDLE LAST GASP (Tier 2 safety - grants one final turn for summary)
            if limit_status == "LAST_GASP":
                if is_trunk:
                    gasp_msg = (
                        "[CRITICAL]: Context Exhaustion Imminent. You have ONE turn remaining. "
                        "You MUST use `fold_context` to synthesize your progress and compress your history now."
                    )
                else:
                    gasp_msg = (
                        "[CRITICAL]: Context Exhaustion Imminent. You have ONE turn remaining. "
                        "You MUST use the DELTA PATTERN to synthesize your progress and `fold_context`, `complete_task`, or `suspend_task` now: "
                        "1. State Delta (what changed), 2. Negative Knowledge (what failed), 3. Handoff (exact next step)."
                    )
                agent_state.amend_last_tool_message(active_task_id, gasp_msg)

            agent_state.append_task_message(active_task_id, message.model_dump(exclude_unset=True))
            if message.tool_calls:
                # Heuristic loop detection (no kill, just warning for HUD)
                detect_cognitive_loop(message.tool_calls, state)

                context_switch, hibernating = _route_tool_calls(message, active_task_id, state, queue, is_trunk)
                if context_switch or hibernating:
                    continue
            else:
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