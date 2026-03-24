"""
Planning Accuracy Test Suite for Ouroboros Self-Evaluation.

Tests evaluate the agent's ability to:
- Create coherent, actionable plans
- Execute plans with minimal deviation
- Adapt plans when circumstances change
- Estimate time and effort accurately
"""

from typing import List, Dict, Any
from .core import TestSuite, TestResult, TestStatus
from .metrics import PlanningMetrics


class PlanningTestSuite(TestSuite):
    """Test suite for planning accuracy evaluation."""
    
    CATEGORY = "planning"
    
    def __init__(self):
        super().__init__("Planning Accuracy Tests")
        self._setup_tests()
    
    def _setup_tests(self) -> None:
        """Configure test cases."""
        self.tests = [
            {
                "id": "plan_simple_task",
                "name": "Simple Task Planning",
                "description": "Plan a straightforward task with clear steps",
                "difficulty": "easy",
                "max_score": 100.0,
                "task": {
                    "goal": "Create a Python file with a greeting function",
                    "constraints": ["Must be valid Python", "Must include docstring"]
                }
            },
            {
                "id": "plan_multi_step",
                "name": "Multi-Step Task Planning",
                "description": "Plan a complex task requiring multiple dependent steps",
                "difficulty": "medium",
                "max_score": 100.0,
                "task": {
                    "goal": "Create a test suite with coverage requirements",
                    "constraints": ["Need to read existing code", "Write tests", "Run tests"]
                }
            },
            {
                "id": "plan_adaptive",
                "name": "Adaptive Planning",
                "description": "Plan that requires adaptation to unexpected conditions",
                "difficulty": "hard",
                "max_score": 100.0,
                "task": {
                    "goal": "Implement feature with potential conflicts",
                    "constraints": ["May encounter existing implementations", "Must handle conflicts"]
                }
            },
            {
                "id": "plan_estimation",
                "name": "Planning Estimation",
                "description": "Estimate time and complexity for a task",
                "difficulty": "medium",
                "max_score": 100.0,
                "task": {
                    "goal": "Refactor module for better maintainability",
                    "constraints": ["Must estimate effort", "Must identify dependencies"]
                }
            },
            {
                "id": "plan_error_recovery",
                "name": "Error Recovery Planning",
                "description": "Plan recovery from failure scenarios",
                "difficulty": "hard",
                "max_score": 100.0,
                "task": {
                    "goal": "Recover from failed file operation",
                    "constraints": ["Must handle partial state", "Must ensure consistency"]
                }
            }
        ]
    
    def run_test(self, test_config: Dict[str, Any]) -> TestResult:
        """Execute a planning test."""
        result = TestResult(
            test_id=test_config["id"],
            test_name=test_config["name"],
            category=self.CATEGORY,
            status=TestStatus.RUNNING,
            max_score=test_config.get("max_score", 100.0)
        )
        
        task = test_config.get("task", {})
        
        # Simulate planning evaluation
        # In a real implementation, this would analyze actual plan generation
        
        try:
            # Generate simulated plan based on task complexity
            plan = self._generate_plan(task)
            execution_log = self._simulate_execution(plan, task)
            
            # Calculate metrics
            metrics = PlanningMetrics.calculate_planning_score(plan, execution_log)
            result.score = metrics.get("total", 0.0)
            result.metrics = metrics
            
            # Add details
            result.details = f"Plan depth: {plan.get('depth', 0)}, " \
                           f"Steps completed: {metrics['step_completion_rate']:.1f}/40"
            
            if result.score >= result.max_score * 0.9:
                result.status = TestStatus.PASSED
            else:
                result.status = TestStatus.FAILED
                
        except Exception as e:
            result.status = TestStatus.ERROR
            result.error_message = str(e)
        
        return result
    
    def _generate_plan(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a simulated plan for the given task."""
        goal = task.get("goal", "")
        constraints = task.get("constraints", [])
        
        # Determine plan complexity based on task
        difficulty_keywords = {
            "easy": 3,
            "medium": 5,
            "hard": 8
        }
        
        # Default to medium complexity
        num_steps = 5
        
        steps = []
        for i in range(num_steps):
            step = {
                "step_number": i + 1,
                "description": f"Step {i + 1} of plan for: {goal[:30]}...",
                "status": "planned",
                "estimated_time": (i + 1) * 2  # Simulated time estimate
            }
            
            # Add subtasks for complex steps
            if i > 1:
                step["subtasks"] = [
                    {"id": f"sub_{i}_1", "description": "Subtask 1"},
                    {"id": f"sub_{i}_2", "description": "Subtask 2"}
                ]
            
            steps.append(step)
        
        return {
            "goal": goal,
            "constraints": constraints,
            "steps": steps,
            "depth": len(steps),
            "estimated_time": sum(s.get("estimated_time", 0) for s in steps),
            "created_at": "simulated"
        }
    
    def _simulate_execution(
        self, 
        plan: Dict[str, Any], 
        task: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Simulate execution of a plan."""
        execution_log = []
        
        for step in plan.get("steps", []):
            log_entry = {
                "step": step["step_number"],
                "completed": True,  # Simulate success
                "execution_time": step.get("estimated_time", 0) * 0.9,  # Slightly faster
                "adapted": False
            }
            
            execution_log.append(log_entry)
        
        return execution_log
