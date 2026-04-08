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
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
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


from core_registry import registry

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

_PATCH_FAILURE_STREAK = 0

@registry.tool(
    description="Surgical edit. Replaces a specific block of text in a file. If patch_file fails, consider using the AST-based replace_symbol tool for smaller changes (functions/classes) or write_file for bigger changes.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}, "search_text": {"type": "string"}, "replace_text": {"type": "string"}}, "required": ["path", "search_text", "replace_text"]},
    bucket="filesystem"
)
def patch_file(args):
    global _PATCH_FAILURE_STREAK

    if _PATCH_FAILURE_STREAK >= 3:
        _PATCH_FAILURE_STREAK = 0 # Reset for the next task
        return "[SYSTEM OVERRIDE]: patch_file has failed 3 consecutive times due to strict matching errors. Stop using patch_file for this task. You MUST use replace_symbol for smaller changes and write_file for bigger changes."

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
            _PATCH_FAILURE_STREAK += 1
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
            _PATCH_FAILURE_STREAK += 1
            return f"Error: 'search_text' appears {occurrence_count} times. Please provide a more unique block."

        _PATCH_FAILURE_STREAK = 0
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
    description="AST-aware surgical edit. Replaces an entire function or class by its name. Use this instead of patch_file for Python code.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the Python file."},
            "symbol_name": {"type": "string", "description": "The exact name of the function or class to replace (e.g., 'bash_command')."},
            "new_code": {"type": "string", "description": "The complete new code for the function or class, including its def/class declaration."}
        },
        "required": ["path", "symbol_name", "new_code"]
    },
    bucket="filesystem"
)
def replace_symbol(args):
    try:
        file_path = _resolve_safe_path(args.get("path", ""))
        if not file_path.exists() or not file_path.suffix == ".py":
            return f"Error: File '{file_path.name}' does not exist or is not a Python file."

        symbol_name = args.get("symbol_name", "")
        new_code = args.get("new_code", "")

        # Verify the new code is valid Python before attempting anything
        try:
            _validate_python_syntax(new_code)
        except SyntaxError as e:
            return f"SYSTEM REJECTED: Your new_code contains invalid Python syntax.\nError: {e.msg} at line {e.lineno}"

        source_bytes = file_path.read_bytes()
        tree = parser.parse(source_bytes)

        # Query for both functions and classes matching the name
        query_str = f"""
            (function_definition name: (identifier) @name (#eq? @name "{symbol_name}")) @target
            (class_definition name: (identifier) @name (#eq? @name "{symbol_name}")) @target
        """
        try:
            query = Query(PY_LANGUAGE, query_str)
            cursor = QueryCursor(query)
            captures_dict = cursor.captures(tree.root_node)
        except Exception as e:
            return f"SYSTEM ERROR parsing AST query: {e}"

        target_node = None
        for capture_name, nodes in captures_dict.items():
            if capture_name == "target":
                target_node = nodes[0]
                break

        if not target_node:
            return f"Error: Could not find function or class named '{symbol_name}' in {file_path.name}."

        # Perform the byte-level splice
        start = target_node.start_byte
        end = target_node.end_byte

        new_source_bytes = source_bytes[:start] + new_code.encode('utf-8') + source_bytes[end:]
        new_source_str = new_source_bytes.decode('utf-8')

        # Final validation of the entire stitched file
        try:
            _validate_python_syntax(new_source_str)
        except SyntaxError as e:
            return f"SYSTEM REJECTED: The stitched file contains invalid Python syntax. Check your indentation relative to the surrounding code."

        file_path.write_text(new_source_str, encoding="utf-8")
        return f"Success: Surgically replaced '{symbol_name}' via AST targeting in {file_path.name}."

    except PermissionError as e: return f"Error: {e}"
    except Exception as e: return f"Error replacing symbol: {e}"

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
    description="Generate a high-signal structural map of the codebase using AST parsing. Use this to understand file structures before attempting surgical patches.",
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
    description="Compress the active execution log when physical context limits are reached. You MUST provide a dense synthesis of the dropped history using the DELTA PATTERN: 1. State Delta (variables/files modified), 2. Negative Knowledge (failed approaches), 3. Handoff (exact next action). Use `store_memory` BEFORE calling this for permanent facts.",
    parameters={
        "type": "object",
        "properties": {
            "synthesis": {
                "type": "string",
                "description": "The highly detailed DELTA PATTERN summary (State Delta, Negative Knowledge, Handoff)."
            }
        },
        "required": ["synthesis"]
    },
    bucket="context_control"
)
def fold_context(args: dict) -> str:
    # Hardcoded to the single timeline — there are no branches.
    log_path = constants.MEMORY_DIR / "task_log_singular_stream.jsonl"

    if not log_path.exists():
        return "Error: Timeline log not found."

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        return f"Error reading log: {e}"

    if len(messages) <= 4:
        return "Fold unnecessary. Context is already minimal."

    # Head: Genesis User message + first Assistant response (immutable anchor)
    head = messages[:2]

    # We don't need a Tail here because the 'Atomic Flush' in _route_tool_calls
    # will append the current Assistant message (the one that called fold_context)
    # and the Tool response itself immediately after this function returns.
    # This ensures the 'SYSTEM AUTONOMIC REFLEX' user message is never preserved.
    preserved = head
    turns_dropped = (len(messages) - len(preserved)) // 2

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            for msg in preserved:
                f.write(json.dumps(msg) + "\n")

        # Reset turn counter and clear the force_fold flag (Finding 15)
        state = agent_state.load_state()
        state["timeline_turns"] = 1
        state["force_fold"] = False
        state["last_context_size"] = 1000 # Reset estimate (Finding 17)
        agent_state.save_state(state)

    except Exception as e:
        return f"Error writing log during fold: {e}"

    return f"Fold successful. {turns_dropped} turns amputated. Synthesis anchored. Context reset."


@registry.tool(
    description="Send a Telegram message to the creator. P4 Authenticity: Telegram Markdown is fragile and often causes Error 400. PREFER PLAIN TEXT. Avoid bold, italics, or complex symbols in long messages. One-sentence updates only.",
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
        # P4 Authenticity: Send as plain text first to avoid parser errors (Finding 13)
        r = requests.post(
            f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
        if r.status_code == 200:
            agent_state.append_chat_history("Ouroboros", text)
            return "Message sent successfully."

        # Fallback: Strip characters if it was a formatting error (though we removed parse_mode)
        return f"Telegram Error {r.status_code}: {r.text}"
    except Exception as e: return f"Telegram failure: {e}"

def _run_git_command(args: List[str]) -> Tuple[int, str, str]:
    """Run git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=300
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
        # P1 Continuity: Ensure the agent sees the auditor's rejection reason
        full_output = (stdout + "\n" + stderr).strip()
        return f"Commit failed:\n{full_output}"

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
    description="Remove a completed or obsolete task from the queue by its task_id. Use after handling an interrupt or administrative task.",
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
    synthesis = args.get("synthesis", "No synthesis provided.")

    q = agent_state.load_task_queue()
    if not q:
        return "Error: Queue is empty — nothing to dismiss."

    task_id = args.get("task_id")
    agent_state.append_task_archive(task_id, synthesis)

    q = [t for t in q if t.get("task_id") != task_id]
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2), encoding="utf-8")

    return f"Task {task_id} dismissed from queue."

@registry.tool(
    description="Queue a new task. Use this to break down complex objectives. ALWAYS check your PENDING QUEUE in the system prompt first to ensure you aren't adding a duplicate task. Omit run_after_timestamp for immediate queueing, or provide a UNIX timestamp to defer.",
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "priority": {"type": "integer"},
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

    if run_after is not None:
        try:
            run_after = float(run_after)
        except (ValueError, TypeError):
            return "Error: 'run_after_timestamp' must be a valid UNIX timestamp."

        scheduled = []
        if constants.SCHEDULED_TASKS_PATH.exists():
            try:
                content = constants.SCHEDULED_TASKS_PATH.read_text(encoding="utf-8").strip()
                if content: scheduled = json.loads(content)
            except Exception:
                pass

        tid = f"task_future_{int(time.time())}"
        scheduled.append({"task_id": tid, "description": description, "priority": priority, "run_after": run_after})
        constants.SCHEDULED_TASKS_PATH.write_text(json.dumps(scheduled, indent=2), encoding="utf-8")
        time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run_after))
        return f"Scheduled {tid} to activate after {time_str}."

    # Regular task queue
    q = agent_state.load_task_queue()
    tid = f"task_{int(time.time())}"
    task_obj = {"task_id": tid, "description": description, "priority": priority}

    q.append(task_obj)
    q.sort(key=lambda x: x.get("priority", 1), reverse=True)
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    return f"Queued {tid} with priority {priority}."

@registry.tool(
    description="Complete and archive the current top task in your queue. Use this ONLY when the objective is 100% met. You MUST provide an autopsy using the DELTA PATTERN: 1. State Delta (variables/files modified), 2. Negative Knowledge (failed approaches), 3. Handoff (exact next action).",
    parameters={
        "type": "object",
        "properties": {
            "synthesis": {"type": "string", "description": "Autopsy following the DELTA PATTERN: 1. State Delta, 2. Negative Knowledge, 3. Handoff."}
        },
        "required": ["synthesis"]
    },
    bucket="global"
)
def complete_task(args):
    synthesis = args.get("synthesis", "No synthesis provided.")
    q = agent_state.load_task_queue()

    if not q:
        return "Error: Queue is empty."

    completed_task = q.pop(0)
    task_id = completed_task.get("task_id", "unknown_task")

    agent_state.append_task_archive(task_id, synthesis)
    constants.TASK_QUEUE_PATH.write_text(json.dumps(q, indent=2))

    return f"Active task '{completed_task.get('description')}' completed and removed from queue."

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
    description="Show staged git diff before committing. Returns the diff of files staged for commit, truncated to 4000 characters if too long.",
    parameters={"type": "object", "properties": {}},
    bucket="git_operations"
)
def git_diff(args):
    try:
        result = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout
        if result.stderr:
            output = f"STDERR: {result.stderr}\n\n{output}"
        
        if len(output) > 4000:
            output = output[:4000] + "\n\n[DIFF TRUNCATED - exceeds 4000 chars]"
        
        return output if output.strip() else "No staged changes to show."
    except subprocess.TimeoutExpired:
        return "Error: git diff command timed out."
    except Exception as e:
        return f"Error running git diff: {e}"

@registry.tool(
    description="Reflect on the current state. CRITICAL: Do NOT use this tool to simply say 'I am waiting' or to acknowledge an empty queue. If your queue is empty, you MUST use `push_task` to set a new proactive goal, `generate_repo_map` to explore the codebase, or `store_memory` to synthesize insights. Only use `reflect` with status='standby' if you have exhaustively completed all optimizations and memory management.",
    parameters={
        "type": "object",
        "properties": {
            "reflection": {"type": "string", "description": "Internal monologue, synthesis of findings, or reasoning about the current situation."},
            "status": {"type": "string", "description": "Optional status update (e.g., 'continuing', 'stuck', 'pivoting', 'standby')."}
        },
        "required": ["reflection"]
    },
    bucket="system_control"
)
def reflect(args: dict) -> str:
    status = args.get("status", "continuing")

    if status.lower() == "standby":
        # Handle the sleep logic internally that hibernate used to do
        duration = 120
        state = agent_state.load_state()
        state["wake_time"] = time.time() + duration
        if "sys_temp" in state: del state["sys_temp"]
        if "sys_think" in state: del state["sys_think"]
        agent_state.save_state(state)
        return f"[SYSTEM: Standby sequence engaged. Wake-up scheduled in {duration}s.] SYSTEM_SIGNAL_HIBERNATE:{duration}"

    return f"Reflection logged. System status: {status}. You may proceed with the next action."


@registry.tool(
    description="Signal the watchdog to restart the agent process. Use this AFTER committing your changes via bash_command('git commit'). The git pre-commit hook enforces mypy and pytest automatically — if those fail, the commit (and therefore this restart) will be blocked.",
    parameters={"type": "object", "properties": {}},
    bucket="system_control"
)
def request_restart(args):
    return "SYSTEM_SIGNAL_RESTART"
