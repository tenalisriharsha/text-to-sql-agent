"""Tests for AuditTrace: in-memory recording and the JSON Lines file format."""

from __future__ import annotations

import json
from pathlib import Path

from text_to_sql_agent.audit import AuditTrace


def test_record_without_path_stays_in_memory_only(tmp_path: Path):
    trace = AuditTrace()
    step = trace.record("question_received", question="how many orders")

    assert step.step_type == "question_received"
    assert step.payload == {"question": "how many orders"}
    assert step.timestamp
    assert trace.to_list() == [step.to_dict()]
    # no path was given, so nothing should have been written anywhere.
    assert list(tmp_path.iterdir()) == []


def test_record_with_path_creates_parent_directories_and_appends_json_lines(tmp_path: Path):
    log_path = tmp_path / "nested" / "dir" / "trace.jsonl"
    trace = AuditTrace(log_path)

    trace.record("question_received", question="how many customers")
    trace.record("final", outcome="executed", row_count=1)

    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["step_type"] == "question_received"
    assert first["payload"]["question"] == "how many customers"

    second = json.loads(lines[1])
    assert second["step_type"] == "final"
    assert second["payload"]["row_count"] == 1


def test_steps_preserve_order_and_accumulate_across_record_calls():
    trace = AuditTrace()
    for step_type in ("question_received", "draft", "validation", "final"):
        trace.record(step_type)

    assert [step.step_type for step in trace.steps] == [
        "question_received",
        "draft",
        "validation",
        "final",
    ]


def test_to_list_returns_plain_dicts_matching_recorded_steps():
    trace = AuditTrace()
    trace.record("draft", attempt=1, sql="SELECT 1")

    [entry] = trace.to_list()
    assert entry == {
        "step_type": "draft",
        "timestamp": entry["timestamp"],
        "payload": {"attempt": 1, "sql": "SELECT 1"},
    }


def test_load_reads_back_exactly_what_was_written(tmp_path: Path):
    log_path = tmp_path / "trace.jsonl"
    trace = AuditTrace(log_path)
    trace.record("question_received", question="q1")
    trace.record("draft", attempt=1, sql="SELECT 1")

    loaded = AuditTrace.load(log_path)
    assert loaded == trace.to_list()


def test_load_skips_blank_lines(tmp_path: Path):
    log_path = tmp_path / "trace.jsonl"
    log_path.write_text('{"step_type": "a", "timestamp": "t", "payload": {}}\n\n')

    loaded = AuditTrace.load(log_path)
    assert loaded == [{"step_type": "a", "timestamp": "t", "payload": {}}]


def test_record_writes_non_ascii_payload_as_readable_utf8(tmp_path: Path):
    log_path = tmp_path / "trace.jsonl"
    trace = AuditTrace(log_path)
    trace.record("draft", sql="SELECT 'café' AS name")

    raw = log_path.read_text(encoding="utf-8")
    assert "café" in raw
    assert "\\u00e9" not in raw
