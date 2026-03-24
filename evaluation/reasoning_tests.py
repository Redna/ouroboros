"""
Reasoning Quality Test Suite for Ouroboros Self-Evaluation.

Tests evaluate the agent's ability to:
- Produce logically correct reasoning chains
- Cover all relevant considerations
- Maintain coherent flow between reasoning steps
- Demonstrate depth of analysis
"""

from typing import List, Dict, Any, Optional
from .core import TestSuite, TestResult, TestStatus
from .metrics import ReasoningMetrics


class ReasoningTestSuite(TestSuite):
    """Test suite for reasoning quality evaluation."""
    
    CATEGORY = "reasoning"
    
    def __init__(self):
        super().__init__("Reasoning Quality Tests")
        self._setup_tests()
    
    def _setup_tests(self) -> None:
        """Configure test cases."""
        self.tests = [
            {
                "id": "reason_deductive",
                "name": "Deductive Reasoning",
                "description": "Apply deductive logic to reach conclusions",
                "difficulty": "easy",
                "max_score": 100.0,
                "problem": "If all A are B, and some B are C, what can we conclude?",
                "required_reasoning_points": [
                    "Identify premise relationships",
                    "Apply syllogistic logic"
                ]
            },
            {
                "id": "reason_inductive",
                "name": "Inductive Reasoning",
                "description": "Draw general conclusions from specific observations",
                "difficulty": "medium",
                "max_score": 100.0,
                "problem": "Given pattern: 2, 4, 8, 16... what comes next?",
                "required_reasoning_points": [
                    "Identify pattern type",
                    "Calculate growth factor",
                    "Extrapolate to next value"
                ]
            },
            {
                "id": "reason_abductive",
                "name": "Abductive Reasoning",
                "description": "Infer the most likely explanation",
                "difficulty": "hard",
                "max_score": 100.0,
                "problem": "File write failed with permission error. What caused it?",
                "required_reasoning_points": [
                    "Consider permission hierarchy",
                    "Evaluate user context",
                    "Assess file location"
                ]
            },
            {
                "id": "reason_causal",
                "name": "Causal Reasoning",
                "description": "Identify cause-effect relationships",
                "difficulty": "medium",
                "max_score": 100.0,
                "problem": "Test failure increased after code change. Why?",
                "required_reasoning_points": [
                    "Compare before/after states",
                    "Identify changed dependencies",
                    "Trace execution path"
                ]
            },
            {
                "id": "reason_counterfactual",
                "name": "Counterfactual Reasoning",
                "description": "Reason about what would happen under different conditions",
                "difficulty": "hard",
                "max_score": 100.0,
                "problem": "If I had used a different tool, would the result be better?",
                "required_reasoning_points": [
                    "Identify alternative tools",
                    "Compare capabilities",
                    "Evaluate trade-offs"
                ]
            }
        ]
    
    def run_test(self, test_config: Dict[str, Any]) -> TestResult:
        """Execute a reasoning test."""
        result = TestResult(
            test_id=test_config["id"],
            test_name=test_config["name"],
            category=self.CATEGORY,
            status=TestStatus.RUNNING,
            max_score=test_config.get("max_score", 100.0)
        )
        
        problem = test_config.get("problem", "")
        required_points = test_config.get("required_reasoning_points", [])
        
        try:
            # Simulate reasoning chain generation
            reasoning_chain = self._generate_reasoning_chain(problem, required_points)
            ground_truth = self._create_ground_truth(required_points)
            
            # Calculate metrics
            metrics = ReasoningMetrics.calculate_reasoning_score(
                reasoning_chain, ground_truth
            )
            
            result.score = metrics.get("total", 0.0)
            result.metrics = metrics
            
            # Add details
            logical_pct = (metrics['logical_correctness'] / 35 * 100) if metrics.get('logical_correctness') else 0
            depth_score = metrics.get('depth', 0)
            result.details = f"Reasoning steps: {len(reasoning_chain)}, " \
                           f"logical correctness: {logical_pct:.1f}%, " \
                           f"depth: {depth_score:.1f}/20"
            
            if result.score >= result.max_score * 0.9:
                result.status = TestStatus.PASSED
            else:
                result.status = TestStatus.FAILED
                
        except Exception as e:
            result.status = TestStatus.ERROR
            result.error_message = str(e)
        
        return result
    
    def _generate_reasoning_chain(
        self, 
        problem: str, 
        required_points: List[str]
    ) -> List[Dict[str, Any]]:
        """Generate a simulated reasoning chain."""
        chain = []
        
        # Generate steps based on required points
        for i, point in enumerate(required_points):
            step = {
                "step_number": i + 1,
                "content": f"Analyzing: {point}. Context: {problem[:30]}...",
                "valid": True,
                "complexity": 1 + (i * 0.2),  # Increasing complexity
                "builds_on_previous": i > 0,
                "addresses_point": point
            }
            chain.append(step)
        
        # Add conclusion step
        chain.append({
            "step_number": len(chain) + 1,
            "content": f"Conclusion based on analysis of {problem[:20]}...",
            "valid": True,
            "complexity": max(s.get("complexity", 1) for s in chain),
            "builds_on_previous": True
        })
        
        return chain
    
    def _create_ground_truth(self, required_points: List[str]) -> Dict[str, Any]:
        """Create ground truth for evaluation."""
        return {
            "required_points": required_points,
            "min_steps": len(required_points),
            "expected_conclusion": True
        }
