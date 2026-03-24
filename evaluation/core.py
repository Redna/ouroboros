"""
Core Evaluation Engine for Ouroboros Self-Evaluation

Provides the foundation for running, scoring, and aggregating test results
across all cognitive dimensions.
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from pathlib import Path
from enum import Enum


class TestStatus(Enum):
    """Status of a test execution."""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class TestResult:
    """Result of a single test execution."""
    test_id: str
    test_name: str
    category: str
    status: TestStatus = TestStatus.PENDING
    score: float = 0.0
    max_score: float = 100.0
    metrics: Dict[str, Any] = None
    execution_time: float = 0.0
    error_message: Optional[str] = None
    details: Optional[str] = None
    
    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "category": self.category,
            "status": self.status.value,
            "score": self.score,
            "max_score": self.max_score,
            "metrics": self.metrics,
            "execution_time": self.execution_time,
            "error_message": self.error_message,
            "details": self.details
        }
    
    @property
    def pass_rate(self) -> float:
        """Calculate pass rate (0-1) for this test."""
        if self.max_score == 0:
            return 0.0
        return min(1.0, self.score / self.max_score)


@dataclass
class CategorySummary:
    """Summary of results for a single category."""
    category: str
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    skipped_tests: int = 0
    total_score: float = 0.0
    max_possible_score: float = 0.0
    average_score: float = 0.0
    
    @property
    def pass_rate(self) -> float:
        """Calculate pass rate for this category."""
        if self.total_tests == 0:
            return 0.0
        return self.passed_tests / self.total_tests
    
    @property
    def score_percentage(self) -> float:
        """Calculate score percentage for this category."""
        if self.max_possible_score == 0:
            return 0.0
        return (self.total_score / self.max_possible_score) * 100


@dataclass
class EvaluationReport:
    """Complete evaluation report with all results and summaries."""
    evaluation_id: str
    timestamp: str
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    skipped_tests: int = 0
    overall_score: float = 0.0
    category_summaries: List[CategorySummary] = None
    test_results: List[TestResult] = None
    
    def __post_init__(self):
        if self.category_summaries is None:
            self.category_summaries = []
        if self.test_results is None:
            self.test_results = []
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary."""
        return {
            "evaluation_id": self.evaluation_id,
            "timestamp": self.timestamp,
            "total_tests": self.total_tests,
            "passed_tests": self.passed_tests,
            "failed_tests": self.failed_tests,
            "skipped_tests": self.skipped_tests,
            "overall_score": self.overall_score,
            "category_summaries": [asdict(s) for s in self.category_summaries],
            "test_results": [t.to_dict() for t in self.test_results]
        }
    
    def to_json(self, indent: int = 2) -> str:
        """Convert report to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


class TestSuite:
    """
    Base class for test suites targeting specific cognitive dimensions.
    """
    
    CATEGORY: str = "base"
    
    def __init__(self, name: str):
        self.name = name
        self.tests: List[Dict[str, Any]] = []
    
    def get_tests(self) -> List[Dict[str, Any]]:
        """Return list of test configurations."""
        return self.tests
    
    def run_test(self, test_config: Dict[str, Any]) -> TestResult:
        """Execute a single test. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement run_test")


class ScoringEngine:
    """Engine for calculating and aggregating scores."""
    
    DEFAULT_WEIGHTS = {
        "planning": 0.25,
        "memory": 0.25,
        "tools": 0.25,
        "reasoning": 0.25
    }
    
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS
    
    def calculate_test_score(self, result: TestResult, max_score: float = 100.0) -> float:
        """Calculate normalized score for a single test (0-100)."""
        if result.status == TestStatus.PASSED:
            return min(100.0, result.score)
        return 0.0
    
    def calculate_category_score(self, results: List[TestResult], category: str) -> CategorySummary:
        """Calculate summary statistics for a category."""
        summary = CategorySummary(category=category)
        
        for result in results:
            if result.category != category:
                continue
            
            summary.total_tests += 1
            summary.max_possible_score += result.max_score
            
            if result.status == TestStatus.PASSED:
                summary.passed_tests += 1
                summary.total_score += self.calculate_test_score(result)
            elif result.status == TestStatus.FAILED:
                summary.failed_tests += 1
            elif result.status == TestStatus.SKIPPED:
                summary.skipped_tests += 1
        
        if summary.total_tests > 0:
            summary.average_score = summary.total_score / summary.total_tests
        
        return summary
    
    def calculate_overall_score(self, category_summaries: List[CategorySummary]) -> float:
        """Calculate weighted overall score (0-100)."""
        if not category_summaries:
            return 0.0
        
        total_weighted_score = 0.0
        total_weight = 0.0
        
        for summary in category_summaries:
            weight = self.weights.get(summary.category, 0.25)
            category_pct = summary.score_percentage
            
            if summary.max_possible_score > 0:
                total_weighted_score += weight * category_pct
                total_weight += weight
        
        if total_weight == 0:
            return 0.0
        
        return (total_weighted_score / total_weight)


class EvaluationEngine:
    """Main evaluation engine that orchestrates test execution and reporting."""
    
    def __init__(self, output_dir: Optional[Path] = None, scoring_weights: Optional[Dict[str, float]] = None):
        self.output_dir = output_dir or Path("/app/evaluation/results")
        self.scoring_engine = ScoringEngine(scoring_weights)
        self.test_suites: List[TestSuite] = []
        self.results: List[TestResult] = []
        self.evaluation_id = f"eval_{int(time.time())}"
    
    def register_suite(self, suite: TestSuite) -> None:
        """Register a test suite for evaluation."""
        self.test_suites.append(suite)
    
    def run_all_tests(self) -> EvaluationReport:
        """Execute all registered test suites and generate report."""
        start_time = time.time()
        
        for suite in self.test_suites:
            for test_config in suite.get_tests():
                result = self._execute_test(suite, test_config)
                self.results.append(result)
        
        report = self._generate_report()
        report.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time))
        self._save_results(report)
        
        return report
    
    def _execute_test(self, suite: TestSuite, test_config: Dict[str, Any]) -> TestResult:
        """Execute a single test with error handling."""
        test_id = test_config.get("id", f"test_{len(self.results)}")
        test_name = test_config.get("name", "Unnamed Test")
        
        result = TestResult(
            test_id=test_id,
            test_name=test_name,
            category=suite.CATEGORY,
            status=TestStatus.RUNNING
        )
        
        start_time = time.time()
        
        try:
            result = suite.run_test(test_config)
        except Exception as e:
            result.status = TestStatus.ERROR
            result.error_message = str(e)
        
        result.execution_time = time.time() - start_time
        
        if result.status == TestStatus.RUNNING:
            if result.score >= result.max_score * 0.9:
                result.status = TestStatus.PASSED
            else:
                result.status = TestStatus.FAILED
        
        return result
    
    def _generate_report(self) -> EvaluationReport:
        """Generate comprehensive evaluation report."""
        report = EvaluationReport(
            evaluation_id=self.evaluation_id,
            timestamp="",
            total_tests=len(self.results),
            test_results=self.results
        )
        
        for result in self.results:
            if result.status == TestStatus.PASSED:
                report.passed_tests += 1
            elif result.status == TestStatus.FAILED:
                report.failed_tests += 1
            elif result.status == TestStatus.SKIPPED:
                report.skipped_tests += 1
        
        categories = set(r.category for r in self.results)
        for category in categories:
            category_results = [r for r in self.results if r.category == category]
            summary = self.scoring_engine.calculate_category_score(category_results, category)
            report.category_summaries.append(summary)
        
        report.overall_score = self.scoring_engine.calculate_overall_score(report.category_summaries)
        
        return report
    
    def _save_results(self, report: EvaluationReport) -> None:
        """Save evaluation results to output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        report_path = self.output_dir / f"{report.evaluation_id}_report.json"
        with open(report_path, "w") as f:
            f.write(report.to_json())
        
        for result in self.results:
            result_path = self.output_dir / f"{result.test_id}.json"
            with open(result_path, "w") as f:
                f.write(json.dumps(result.to_dict(), indent=2))
