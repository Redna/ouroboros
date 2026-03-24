"""
Report generation and visualization for Ouroboros self-evaluation.

Provides human-readable reports with insights, trends, and actionable feedback.
"""

from typing import List, Dict, Any
from pathlib import Path
from .core import EvaluationReport, TestResult, CategorySummary


class ReportGenerator:
    """Generate comprehensive evaluation reports in various formats."""
    
    def __init__(self, report: EvaluationReport):
        self.report = report
    
    def generate_text_report(self) -> str:
        """Generate a human-readable text report."""
        lines = []
        
        # Header
        lines.append("=" * 80)
        lines.append("OUROBOROS SELF-EVALUATION REPORT")
        lines.append("=" * 80)
        lines.append(f"Evaluation ID: {self.report.evaluation_id}")
        lines.append(f"Timestamp: {self.report.timestamp}")
        lines.append("")
        
        # Overall Summary
        lines.append("OVERALL SUMMARY")
        lines.append("-" * 40)
        lines.append(f"Total Tests: {self.report.total_tests}")
        lines.append(f"Passed: {self.report.passed_tests} ({self._calc_pct(self.report.passed_tests, self.report.total_tests)}%)")
        lines.append(f"Failed: {self.report.failed_tests} ({self._calc_pct(self.report.failed_tests, self.report.total_tests)}%)")
        lines.append(f"Skipped: {self.report.skipped_tests} ({self._calc_pct(self.report.skipped_tests, self.report.total_tests)}%)")
        lines.append(f"Overall Score: {self.report.overall_score:.1f}/100")
        lines.append("")
        
        # Category Breakdown
        lines.append("CATEGORY BREAKDOWN")
        lines.append("-" * 40)
        for summary in self.report.category_summaries:
            lines.append(f"\n{summary.category.upper()}:")
            lines.append(f"  Tests: {summary.total_tests}")
            lines.append(f"  Passed: {summary.passed_tests} ({self._calc_pct(summary.passed_tests, summary.total_tests)}%)")
            lines.append(f"  Score: {summary.score_percentage:.1f}%")
            lines.append(f"  Average: {summary.average_score:.1f}")
        lines.append("")
        
        # Test Details
        lines.append("TEST DETAILS")
        lines.append("-" * 40)
        for result in self.report.test_results:
            status_icon = "✓" if result.status.value == "passed" else "✗" if result.status.value == "failed" else "○"
            lines.append(f"\n{status_icon} [{result.category.upper()}] {result.test_name}")
            lines.append(f"   Score: {result.score:.1f}/{result.max_score:.1f}")
            lines.append(f"   Time: {result.execution_time:.2f}s")
            if result.error_message:
                lines.append(f"   Error: {result.error_message}")
        lines.append("")
        
        # Insights
        lines.append("INSIGHTS & RECOMMENDATIONS")
        lines.append("-" * 40)
        insights = self._generate_insights()
        for insight in insights:
            lines.append(f"• {insight}")
        lines.append("")
        
        lines.append("=" * 80)
        lines.append("END OF REPORT")
        lines.append("=" * 80)
        
        return "\n".join(lines)
    
    def generate_json_report(self, indent: int = 2) -> str:
        """Generate a JSON-formatted report."""
        return self.report.to_json(indent)
    
    def generate_markdown_report(self) -> str:
        """Generate a Markdown-formatted report."""
        lines = []
        
        lines.append("# Ouroboros Self-Evaluation Report")
        lines.append("")
        lines.append(f"**Evaluation ID:** `{self.report.evaluation_id}`")
        lines.append(f"**Timestamp:** {self.report.timestamp}")
        lines.append("")
        
        # Overall Summary
        lines.append("## Overall Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Total Tests | {self.report.total_tests} |")
        lines.append(f"| Passed | {self.report.passed_tests} ({self._calc_pct(self.report.passed_tests, self.report.total_tests)}%) |")
        lines.append(f"| Failed | {self.report.failed_tests} ({self._calc_pct(self.report.failed_tests, self.report.total_tests)}%) |")
        lines.append(f"| Skipped | {self.report.skipped_tests} |")
        lines.append(f"| **Overall Score** | **{self.report.overall_score:.1f}/100** |")
        lines.append("")
        
        # Category Breakdown
        lines.append("## Category Breakdown")
        lines.append("")
        lines.append("| Category | Tests | Passed | Score | Avg |")
        lines.append("|----------|-------|--------|-------|-----|")
        for summary in self.report.category_summaries:
            lines.append(
                f"| {summary.category} | {summary.total_tests} | "
                f"{summary.passed_tests} | {summary.score_percentage:.1f}% | "
                f"{summary.average_score:.1f} |"
            )
        lines.append("")
        
        # Test Results
        lines.append("## Test Results")
        lines.append("")
        lines.append("| Test | Category | Score | Status | Time |")
        lines.append("|------|----------|-------|--------|------|")
        for result in self.report.test_results:
            status = "✓" if result.status.value == "passed" else "✗"
            lines.append(
                f"| {result.test_name} | {result.category} | "
                f"{result.score:.1f}/{result.max_score:.1f} | {status} | "
                f"{result.execution_time:.2f}s |"
            )
        lines.append("")
        
        # Insights
        lines.append("## Insights & Recommendations")
        lines.append("")
        insights = self._generate_insights()
        for insight in insights:
            lines.append(f"- {insight}")
        lines.append("")
        
        return "\n".join(lines)
    
    def _calc_pct(self, part: int, total: int) -> float:
        """Calculate percentage."""
        if total == 0:
            return 0.0
        return (part / total) * 100
    
    def _generate_insights(self) -> List[str]:
        """Generate actionable insights from evaluation results."""
        insights = []
        
        # Overall performance insight
        if self.report.overall_score >= 90:
            insights.append("Excellent overall performance! System is operating at peak efficiency.")
        elif self.report.overall_score >= 75:
            insights.append("Good performance with room for improvement in specific areas.")
        elif self.report.overall_score >= 50:
            insights.append("Moderate performance. Focus on strengthening weaker categories.")
        else:
            insights.append("Performance below expectations. Comprehensive review recommended.")
        
        # Category-specific insights
        for summary in self.report.category_summaries:
            if summary.score_percentage < 70:
                insights.append(f"⚠️ {summary.category.upper()} category needs attention (score: {summary.score_percentage:.1f}%)")
            elif summary.score_percentage >= 90:
                insights.append(f"✓ {summary.category.upper()} category performing excellently")
        
        # Test-specific insights
        failed_tests = [r for r in self.report.test_results if r.status.value == "failed"]
        if failed_tests:
            insights.append(f"Review {len(failed_tests)} failed tests for common failure patterns")
        
        # Efficiency insight
        avg_time = sum(r.execution_time for r in self.report.test_results) / max(1, len(self.report.test_results))
        if avg_time > 5.0:
            insights.append(f"Average test execution time ({avg_time:.1f}s) suggests potential optimization opportunities")
        
        return insights
    
    def save_report(
        self, 
        output_path: Path, 
        format: str = "text",
        filename: Optional[str] = None
    ) -> None:
        """Save report to file."""
        if filename is None:
            filename = f"{self.report.evaluation_id}_report"
        
        # Ensure directory exists
        output_path.mkdir(parents=True, exist_ok=True)
        
        if format == "text":
            content = self.generate_text_report()
            filepath = output_path / f"{filename}.txt"
        elif format == "markdown":
            content = self.generate_markdown_report()
            filepath = output_path / f"{filename}.md"
        elif format == "json":
            content = self.generate_json_report()
            filepath = output_path / f"{filename}.json"
        else:
            raise ValueError(f"Unknown format: {format}")
        
        with open(filepath, "w") as f:
            f.write(content)
