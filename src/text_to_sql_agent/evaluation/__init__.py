"""Evaluation harness: a fixed benchmark and a runner that scores it.

`benchmark` defines the question set; `runner` drives each question through
`text_to_sql_agent.agent.run_agent_loop` and scores the outcome. See
`text_to_sql_agent.cli`'s `benchmark` command for the CLI entry point.
"""

from text_to_sql_agent.evaluation.benchmark import BENCHMARK, BenchmarkQuestion
from text_to_sql_agent.evaluation.runner import BenchmarkReport, QuestionResult, run_benchmark

__all__ = [
    "BENCHMARK",
    "BenchmarkQuestion",
    "BenchmarkReport",
    "QuestionResult",
    "run_benchmark",
]
