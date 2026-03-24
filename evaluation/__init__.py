"""
Ouroboros Self-Evaluation Framework (AgentBench-style)

This module provides automated evaluation capabilities for measuring Ouroboros's
performance across multiple cognitive dimensions:
- Planning Accuracy
- Memory Retrieval Effectiveness  
- Tool Use Proficiency
- Reasoning Quality

The framework follows AgentBench principles: quantifiable metrics, reproducible tests,
and comprehensive reporting.
"""

from .core import EvaluationEngine, TestSuite, TestResult, ScoringEngine
from .metrics import (
    PlanningMetrics, 
    MemoryMetrics, 
    ToolUseMetrics, 
    ReasoningMetrics
)
from .reporting import ReportGenerator

__version__ = "1.0.0"
__all__ = [
    "EvaluationEngine",
    "TestSuite", 
    "TestResult",
    "ScoringEngine",
    "PlanningMetrics",
    "MemoryMetrics", 
    "ToolUseMetrics",
    "ReasoningMetrics",
    "ReportGenerator"
]
