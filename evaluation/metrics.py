"""
Metrics definitions and specialized test suites for Ouroboros evaluation.

Implements four core cognitive dimensions:
1. Planning Accuracy - How well plans are created and followed
2. Memory Retrieval - Effectiveness of memory access and usage  
3. Tool Use Proficiency - Correct and efficient tool invocation
4. Reasoning Quality - Logical correctness and depth of reasoning
"""

from typing import List, Dict, Any, Optional
from .core import TestSuite, TestResult, TestStatus


class PlanningMetrics:
    """Metrics for evaluating planning accuracy."""
    
    # Key metrics tracked
    PLANNING_DEPTH = "planning_depth"  # How many levels of subtasks
    STEP_COMPLETION_RATE = "step_completion_rate"  # % of planned steps executed
    ADAPTATION_SCORE = "adaptation_score"  # How well plan adapts to changes
    ESTIMATION_ACCURACY = "estimation_accuracy"  # How accurate time/effort estimates
    
    @classmethod
    def calculate_planning_score(
        cls, 
        plan: Dict[str, Any], 
        execution_log: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Calculate comprehensive planning metrics."""
        
        # Planning depth score (max 25 points)
        depth = cls._measure_depth(plan)
        depth_score = min(25.0, depth * 5)
        
        # Step completion rate (max 40 points)
        completion_rate = cls._calculate_completion_rate(plan, execution_log)
        completion_score = completion_rate * 40
        
        # Adaptation score (max 15 points)
        adaptation = cls._measure_adaptation(execution_log)
        adaptation_score = adaptation * 15
        
        # Estimation accuracy (max 20 points)
        estimation = cls._measure_estimation(plan, execution_log)
        estimation_score = estimation * 20
        
        total_score = depth_score + completion_score + adaptation_score + estimation_score
        
        return {
            cls.PLANNING_DEPTH: depth_score,
            cls.STEP_COMPLETION_RATE: completion_score,
            cls.ADAPTATION_SCORE: adaptation_score,
            cls.ESTIMATION_ACCURACY: estimation_score,
            "total": total_score
        }
    
    @classmethod
    def _measure_depth(cls, plan: Dict[str, Any]) -> int:
        """Measure the depth of planning (number of subtask levels)."""
        if not plan:
            return 0
        
        steps = plan.get("steps", [])
        if not steps:
            return 1
        
        max_depth = 1
        for step in steps:
            if "subtasks" in step:
                step_depth = 1 + len(step["subtasks"])
                max_depth = max(max_depth, step_depth)
        
        return max_depth
    
    @classmethod
    def _calculate_completion_rate(
        cls, 
        plan: Dict[str, Any], 
        execution_log: List[Dict[str, Any]]
    ) -> float:
        """Calculate what percentage of planned steps were completed."""
        if not plan or not plan.get("steps"):
            return 0.0
        
        planned_steps = len(plan["steps"])
        executed_steps = sum(1 for log in execution_log if log.get("completed", False))
        
        if planned_steps == 0:
            return 0.0
        
        return min(1.0, executed_steps / planned_steps)
    
    @classmethod
    def _measure_adaptation(cls, execution_log: List[Dict[str, Any]]) -> float:
        """Measure how well the plan adapted to unexpected events."""
        if not execution_log:
            return 0.5  # Default moderate score
        
        adaptations = sum(1 for log in execution_log if log.get("adapted", False))
        total_events = len(execution_log)
        
        if total_events == 0:
            return 0.5
        
        return min(1.0, adaptations / max(1, total_events))
    
    @classmethod
    def _measure_estimation(cls, plan: Dict[str, Any], execution_log: List[Dict[str, Any]]) -> float:
        """Measure accuracy of time/effort estimates."""
        if not plan or not execution_log:
            return 0.5
        
        estimated = plan.get("estimated_time", 0)
        actual = sum(log.get("execution_time", 0) for log in execution_log)
        
        if estimated == 0 or actual == 0:
            return 0.5
        
        # Score based on how close estimate was to actual
        ratio = min(estimated, actual) / max(estimated, actual)
        return ratio


class MemoryMetrics:
    """Metrics for evaluating memory retrieval effectiveness."""
    
    RETRIEVAL_ACCURACY = "retrieval_accuracy"  # % of retrieved items that are relevant
    RETRIEVAL_SPEED = "retrieval_speed"  # Time to retrieve needed information
    CONTEXT_USAGE = "context_usage"  # How effectively retrieved context is used
    MEMORY_FRESHNESS = "memory_freshness"  # How current the retrieved information is
    
    @classmethod
    def calculate_memory_score(
        cls, 
        query: str, 
        retrieved_items: List[Dict[str, Any]], 
        relevant_items: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Calculate comprehensive memory metrics."""
        
        # Retrieval accuracy (max 40 points)
        accuracy = cls._measure_accuracy(retrieved_items, relevant_items)
        accuracy_score = accuracy * 40
        
        # Retrieval speed (max 20 points) - simulated via item count efficiency
        efficiency = cls._measure_efficiency(retrieved_items, relevant_items)
        speed_score = efficiency * 20
        
        # Context usage (max 25 points)
        usage = cls._measure_context_usage(query, retrieved_items)
        usage_score = usage * 25
        
        # Memory freshness (max 15 points) - simulated
        freshness = cls._measure_freshness(retrieved_items)
        freshness_score = freshness * 15
        
        total_score = accuracy_score + speed_score + usage_score + freshness_score
        
        return {
            cls.RETRIEVAL_ACCURACY: accuracy_score,
            cls.RETRIEVAL_SPEED: speed_score,
            cls.CONTEXT_USAGE: usage_score,
            cls.MEMORY_FRESHNESS: freshness_score,
            "total": total_score
        }
    
    @classmethod
    def _measure_accuracy(
        cls, 
        retrieved_items: List[Dict[str, Any]], 
        relevant_items: List[Dict[str, Any]]
    ) -> float:
        """Measure what percentage of retrieved items are actually relevant."""
        if not retrieved_items:
            return 0.0
        
        relevant_ids = {item.get("id") for item in relevant_items}
        retrieved_relevant = sum(1 for item in retrieved_items if item.get("id") in relevant_ids)
        
        return min(1.0, retrieved_relevant / len(retrieved_items))
    
    @classmethod
    def _measure_efficiency(
        cls, 
        retrieved_items: List[Dict[str, Any]], 
        relevant_items: List[Dict[str, Any]]
    ) -> float:
        """Measure retrieval efficiency (relevance per item retrieved)."""
        if not retrieved_items or not relevant_items:
            return 0.0
        
        relevant_count = len(relevant_items)
        retrieved_count = len(retrieved_items)
        
        # Efficiency is how many relevant items found vs total retrieved
        return min(1.0, relevant_count / max(1, retrieved_count))
    
    @classmethod
    def _measure_context_usage(cls, query: str, retrieved_items: List[Dict[str, Any]]) -> float:
        """Measure how effectively retrieved context matches the query."""
        if not query or not retrieved_items:
            return 0.0
        
        # Simple keyword matching for now
        query_words = set(query.lower().split())
        total_relevance = 0.0
        
        for item in retrieved_items:
            content = item.get("content", "").lower()
            content_words = set(content.split())
            overlap = len(query_words & content_words)
            total_relevance += min(1.0, overlap / max(1, len(query_words)))
        
        return total_relevance / max(1, len(retrieved_items))
    
    @classmethod
    def _measure_freshness(cls, retrieved_items: List[Dict[str, Any]]) -> float:
        """Measure how recent the retrieved information is."""
        if not retrieved_items:
            return 0.5
        
        # Simulated freshness based on timestamp or age field
        timestamps = [item.get("timestamp", 0) for item in retrieved_items if item.get("timestamp")]
        
        if not timestamps:
            return 0.5
        
        avg_timestamp = sum(timestamps) / len(timestamps)
        # Assume higher timestamp = more recent, normalize to 0-1
        return min(1.0, avg_timestamp / 1000000)


class ToolUseMetrics:
    """Metrics for evaluating tool use proficiency."""
    
    TOOL_SELECTION_ACCURACY = "tool_selection_accuracy"  # % of correct tool choices
    PARAMETER_CORRECTNESS = "parameter_correctness"  # % of correctly formed parameters
    ERROR_RATE = "error_rate"  # % of tool calls that result in errors
    EFFICIENCY = "efficiency"  # How few tools needed to accomplish task
    
    @classmethod
    def calculate_tool_score(
        cls, 
        tool_calls: List[Dict[str, Any]], 
        expected_tools: List[str],
        errors: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Calculate comprehensive tool use metrics."""
        
        # Tool selection accuracy (max 30 points)
        selection = cls._measure_selection_accuracy(tool_calls, expected_tools)
        selection_score = selection * 30
        
        # Parameter correctness (max 30 points)
        params = cls._measure_parameter_correctness(tool_calls)
        params_score = params * 30
        
        # Error rate (max 20 points) - inverted (fewer errors = higher score)
        error_rate = cls._calculate_error_rate(tool_calls, errors)
        error_score = (1.0 - error_rate) * 20
        
        # Efficiency (max 20 points)
        efficiency = cls._measure_efficiency(tool_calls, expected_tools)
        efficiency_score = efficiency * 20
        
        total_score = selection_score + params_score + error_score + efficiency_score
        
        return {
            cls.TOOL_SELECTION_ACCURACY: selection_score,
            cls.PARAMETER_CORRECTNESS: params_score,
            cls.ERROR_RATE: error_score,
            cls.EFFICIENCY: efficiency_score,
            "total": total_score
        }
    
    @classmethod
    def _measure_selection_accuracy(
        cls, 
        tool_calls: List[Dict[str, Any]], 
        expected_tools: List[str]
    ) -> float:
        """Measure how many tool calls selected the correct tool."""
        if not tool_calls or not expected_tools:
            return 0.0
        
        correct = sum(1 for call in tool_calls if call.get("tool_name") in expected_tools)
        return min(1.0, correct / len(tool_calls))
    
    @classmethod
    def _measure_parameter_correctness(cls, tool_calls: List[Dict[str, Any]]) -> float:
        """Measure how well parameters are formed for each tool call."""
        if not tool_calls:
            return 0.0
        
        correct = sum(1 for call in tool_calls if call.get("params_valid", True))
        return min(1.0, correct / len(tool_calls))
    
    @classmethod
    def _calculate_error_rate(
        cls, 
        tool_calls: List[Dict[str, Any]], 
        errors: List[Dict[str, Any]]
    ) -> float:
        """Calculate the rate of tool call errors."""
        if not tool_calls:
            return 0.0
        
        return min(1.0, len(errors) / len(tool_calls))
    
    @classmethod
    def _measure_efficiency(
        cls, 
        tool_calls: List[Dict[str, Any]], 
        expected_tools: List[str]
    ) -> float:
        """Measure how efficiently tools were used (minimal calls to achieve goal)."""
        if not tool_calls or not expected_tools:
            return 0.5
        
        # Fewer calls with correct tools = higher efficiency
        optimal_calls = len(expected_tools)
        actual_calls = len(tool_calls)
        
        if actual_calls == 0:
            return 0.0
        
        return min(1.0, max(0, optimal_calls / actual_calls))


class ReasoningMetrics:
    """Metrics for evaluating reasoning quality."""
    
    LOGICAL_CORRECTNESS = "logical_correctness"  % of logically valid steps
    COMPLETENESS = "completeness"  # Coverage of relevant considerations
    COHERENCE = "coherence"  # How well reasoning flows
    DEPTH = "depth"  # Depth of analysis
    
    @classmethod
    def calculate_reasoning_score(
        cls, 
        reasoning_chain: List[Dict[str, Any]], 
        ground_truth: Optional[Dict[str, Any]] = None
    ) -> Dict[str, float]:
        """Calculate comprehensive reasoning metrics."""
        
        # Logical correctness (max 35 points)
        logical = cls._measure_logical_correctness(reasoning_chain)
        logical_score = logical * 35
        
        # Completeness (max 25 points)
        complete = cls._measure_completeness(reasoning_chain, ground_truth)
        completeness_score = complete * 25
        
        # Coherence (max 20 points)
        coherent = cls._measure_coherence(reasoning_chain)
        coherence_score = coherent * 20
        
        # Depth (max 20 points)
        depth = cls._measure_depth(reasoning_chain)
        depth_score = depth * 20
        
        total_score = logical_score + completeness_score + coherence_score + depth_score
        
        return {
            cls.LOGICAL_CORRECTNESS: logical_score,
            cls.COMPLETENESS: completeness_score,
            cls.COHERENCE: coherence_score,
            cls.DEPTH: depth_score,
            "total": total_score
        }
    
    @classmethod
    def _measure_logical_correctness(cls, reasoning_chain: List[Dict[str, Any]]) -> float:
        """Measure logical validity of reasoning steps."""
        if not reasoning_chain:
            return 0.0
        
        valid_steps = sum(1 for step in reasoning_chain if step.get("valid", True))
        return min(1.0, valid_steps / len(reasoning_chain))
    
    @classmethod
    def _measure_completeness(
        cls, 
        reasoning_chain: List[Dict[str, Any]], 
        ground_truth: Optional[Dict[str, Any]]
    ) -> float:
        """Measure coverage of relevant considerations."""
        if not reasoning_chain:
            return 0.0
        
        if ground_truth and ground_truth.get("required_points"):
            covered = sum(1 for step in reasoning_chain 
                         if any(p in str(step.get("content", "")) 
                               for p in ground_truth["required_points"]))
            return min(1.0, covered / len(ground_truth["required_points"]))
        
        # Default: assume complete if chain is substantial
        return min(1.0, len(reasoning_chain) / 10.0)
    
    @classmethod
    def _measure_coherence(cls, reasoning_chain: List[Dict[str, Any]]) -> float:
        """Measure how well reasoning steps flow together."""
        if len(reasoning_chain) < 2:
            return 1.0 if reasoning_chain else 0.0
        
        # Check if steps reference previous steps appropriately
        coherent = 0
        for i in range(1, len(reasoning_chain)):
            current = reasoning_chain[i]
            prev = reasoning_chain[i - 1]
            
            # Simple coherence: check if step builds on previous
            if current.get("builds_on_previous", True):
                coherent += 1
        
        return coherent / max(1, len(reasoning_chain) - 1)
    
    @classmethod
    def _measure_depth(cls, reasoning_chain: List[Dict[str, Any]]) -> float:
        """Measure depth of analysis."""
        if not reasoning_chain:
            return 0.0
        
        # Depth based on number of reasoning steps and their complexity
        avg_complexity = sum(step.get("complexity", 1) for step in reasoning_chain) / len(reasoning_chain)
        
        # Normalize to 0-1 (assume max depth of 20 steps with high complexity)
        raw_depth = len(reasoning_chain) * avg_complexity
        return min(1.0, raw_depth / 20.0)
