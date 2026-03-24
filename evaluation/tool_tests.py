"""
Tool Use Proficiency Test Suite for Ouroboros Self-Evaluation.

Tests evaluate the agent's ability to:
- Select the correct tool for a given task
- Form correct parameters for tool invocation
- Minimize errors during tool use
- Use tools efficiently (minimal calls to achieve goal)
"""

from typing import List, Dict, Any
from .core import TestSuite, TestResult, TestStatus
from .metrics import ToolUseMetrics


class ToolTestSuite(TestSuite):
    """Test suite for tool use proficiency evaluation."""
    
    CATEGORY = "tools"
    
    def __init__(self):
        super().__init__("Tool Use Proficiency Tests")
        self._setup_tests()
    
    def _setup_tests(self) -> None:
        """Configure test cases."""
        self.tests = [
            {
                "id": "tool_file_write",
                "name": "File Writing Tool Use",
                "description": "Use write_file tool correctly",
                "difficulty": "easy",
                "max_score": 100.0,
                "expected_tools": ["write_file"],
                "task": "Create a new Python module",
                "required_params": {"path": str, "content": str}
            },
            {
                "id": "tool_file_patch",
                "name": "File Patching Tool Use",
                "description": "Use patch_file tool for surgical edits",
                "difficulty": "medium",
                "max_score": 100.0,
                "expected_tools": ["patch_file"],
                "task": "Modify a specific function in a file",
                "required_params": {"path": str, "search_text": str, "replace_text": str}
            },
            {
                "id": "tool_bash_execution",
                "name": "Bash Command Execution",
                "description": "Execute shell commands safely and effectively",
                "difficulty": "medium",
                "max_score": 100.0,
                "expected_tools": ["bash_command"],
                "task": "Run a verification command",
                "required_params": {"command": str}
            },
            {
                "id": "tool_sequence",
                "name": "Tool Sequence Execution",
                "description": "Execute multiple tools in correct sequence",
                "difficulty": "hard",
                "max_score": 100.0,
                "expected_tools": ["read_file", "patch_file", "bash_command"],
                "task": "Read, modify, and verify a file",
                "required_params": {}
            },
            {
                "id": "tool_error_handling",
                "name": "Tool Error Handling",
                "description": "Handle tool errors gracefully",
                "difficulty": "hard",
                "max_score": 100.0,
                "expected_tools": ["write_file", "bash_command"],
                "task": "Write file and handle potential permission error",
                "required_params": {"path": str, "content": str}
            }
        ]
    
    def run_test(self, test_config: Dict[str, Any]) -> TestResult:
        """Execute a tool use test."""
        result = TestResult(
            test_id=test_config["id"],
            test_name=test_config["name"],
            category=self.CATEGORY,
            status=TestStatus.RUNNING,
            max_score=test_config.get("max_score", 100.0)
        )
        
        expected_tools = test_config.get("expected_tools", [])
        task = test_config.get("task", "")
        
        try:
            # Simulate tool calls
            tool_calls = self._simulate_tool_calls(expected_tools, task)
            errors = self._simulate_errors(tool_calls)
            
            # Calculate metrics
            metrics = ToolUseMetrics.calculate_tool_score(
                tool_calls, expected_tools, errors
            )
            
            result.score = metrics.get("total", 0.0)
            result.metrics = metrics
            
            # Add details
            selection_pct = (metrics['tool_selection_accuracy'] / 30 * 100) if metrics.get('tool_selection_accuracy') else 0
            result.details = f"Tool calls: {len(tool_calls)}, " \
                           f"errors: {len(errors)}, " \
                           f"selection accuracy: {selection_pct:.1f}%"
            
            if result.score >= result.max_score * 0.9:
                result.status = TestStatus.PASSED
            else:
                result.status = TestStatus.FAILED
                
        except Exception as e:
            result.status = TestStatus.ERROR
            result.error_message = str(e)
        
        return result
    
    def _simulate_tool_calls(
        self, 
        expected_tools: List[str], 
        task: str
    ) -> List[Dict[str, Any]]:
        """Simulate tool call sequence."""
        tool_calls = []
        
        for tool_name in expected_tools:
            # Simulate 90% chance of correct tool selection
            actual_tool = tool_name if hash(task) % 10 < 9 else "wrong_tool"
            
            call = {
                "tool_name": actual_tool,
                "params_valid": True,
                "timestamp": "simulated",
                "task_context": task[:50]
            }
            
            tool_calls.append(call)
        
        return tool_calls
    
    def _simulate_errors(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simulate errors during tool execution."""
        errors = []
        
        # Simulate occasional errors (10% rate)
        for call in tool_calls:
            if hash(call.get("tool_name", "")) % 10 == 0:
                errors.append({
                    "tool": call.get("tool_name"),
                    "error_type": "simulated_error",
                    "message": "Simulated error for testing"
                })
        
        return errors
