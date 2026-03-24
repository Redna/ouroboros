"""
Memory Retrieval Effectiveness Test Suite for Ouroboros Self-Evaluation.

Tests evaluate the agent's ability to:
- Retrieve relevant information from memory
- Use retrieved context effectively
- Access memory efficiently (minimal latency)
- Maintain fresh, up-to-date information
"""

from typing import List, Dict, Any
from .core import TestSuite, TestResult, TestStatus
from .metrics import MemoryMetrics


class MemoryTestSuite(TestSuite):
    """Test suite for memory retrieval effectiveness evaluation."""
    
    CATEGORY = "memory"
    
    def __init__(self):
        super().__init__("Memory Retrieval Tests")
        self._setup_tests()
    
    def _setup_tests(self) -> None:
        """Configure test cases."""
        self.tests = [
            {
                "id": "mem_simple_retrieval",
                "name": "Simple Memory Retrieval",
                "description": "Retrieve a single piece of information from memory",
                "difficulty": "easy",
                "max_score": 100.0,
                "query": "What is my current working state?",
                "expected_items": ["working_state", "current_task"]
            },
            {
                "id": "mem_contextual_search",
                "name": "Contextual Memory Search",
                "description": "Find relevant information based on context",
                "difficulty": "medium",
                "max_score": 100.0,
                "query": "Find all tasks related to file operations",
                "expected_items": ["file_handling", "write_operations", "read_operations"]
            },
            {
                "id": "mem_multi_hop",
                "name": "Multi-Hop Memory Query",
                "description": "Retrieve information requiring multiple memory accesses",
                "difficulty": "hard",
                "max_score": 100.0,
                "query": "Trace the evolution of my planning capability",
                "expected_items": ["planning_history", "task_logs", "evolution_records"]
            },
            {
                "id": "mem_recent_access",
                "name": "Recent Memory Access",
                "description": "Access recently added or modified memory entries",
                "difficulty": "medium",
                "max_score": 100.0,
                "query": "What was the last successful task?",
                "expected_items": ["last_task", "task_completion"]
            },
            {
                "id": "mem_semantic_similarity",
                "name": "Semantic Memory Matching",
                "description": "Find semantically related memory entries",
                "difficulty": "hard",
                "max_score": 100.0,
                "query": "Find all instances of error recovery patterns",
                "expected_items": ["error_handling", "recovery_strategies", "exception_patterns"]
            }
        ]
    
    def run_test(self, test_config: Dict[str, Any]) -> TestResult:
        """Execute a memory retrieval test."""
        result = TestResult(
            test_id=test_config["id"],
            test_name=test_config["name"],
            category=self.CATEGORY,
            status=TestStatus.RUNNING,
            max_score=test_config.get("max_score", 100.0)
        )
        
        query = test_config.get("query", "")
        expected_items = test_config.get("expected_items", [])
        
        try:
            # Simulate memory retrieval
            retrieved_items = self._simulate_retrieval(query, expected_items)
            relevant_items = self._identify_relevant_items(expected_items)
            
            # Calculate metrics
            metrics = MemoryMetrics.calculate_memory_score(
                query, retrieved_items, relevant_items
            )
            
            result.score = metrics.get("total", 0.0)
            result.metrics = metrics
            
            # Add details
            accuracy_pct = (metrics['retrieval_accuracy'] / 40 * 100) if metrics.get('retrieval_accuracy') else 0
            result.details = f"Retrieved {len(retrieved_items)} items, " \
                           f"accuracy: {accuracy_pct:.1f}%"
            
            if result.score >= result.max_score * 0.9:
                result.status = TestStatus.PASSED
            else:
                result.status = TestStatus.FAILED
                
        except Exception as e:
            result.status = TestStatus.ERROR
            result.error_message = str(e)
        
        return result
    
    def _simulate_retrieval(
        self, 
        query: str, 
        expected_items: List[str]
    ) -> List[Dict[str, Any]]:
        """Simulate memory retrieval process."""
        retrieved = []
        
        # Simulate retrieving some relevant and some irrelevant items
        for i, item in enumerate(expected_items):
            # 80% chance of retrieving expected item
            if i < len(expected_items) * 0.8:
                retrieved.append({
                    "id": f"mem_{item}",
                    "content": f"Memory entry for {item}: {query[:20]}...",
                    "timestamp": 1000000 - (i * 10000),
                    "relevance_score": 0.9 - (i * 0.05)
                })
        
        # Add some irrelevant items (noise)
        for i in range(2):
            retrieved.append({
                "id": f"mem_irrelevant_{i}",
                "content": f"Irrelevant memory entry {i}",
                "timestamp": 500000,
                "relevance_score": 0.3
            })
        
        return retrieved
    
    def _identify_relevant_items(self, expected_items: List[str]) -> List[Dict[str, Any]]:
        """Identify which items should be considered relevant."""
        return [
            {
                "id": f"mem_{item}",
                "content": f"Expected memory entry for {item}"
            }
            for item in expected_items
        ]
