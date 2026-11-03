import copy
import json
from pathlib import Path

import pytest

from software_factory import replay_turn
from software_factory.trace_cleaner import clean_conversation, replay_input

FIXTURES = Path(__file__).parent / "fixtures"


def replay(*turns) -> dict:
    return {
        "conversation_id": "c",
        "failing_turn": len(turns),
        "turns": [
            {"turn": n, "timestamp": f"2026-10-29T12:0{n}:00Z", "latency": 1.0, "user_query": q, "answer": a, "has_issues": False}
            for n, (q, a) in enumerate(turns, start=1)
        ],
    }


def test_history_is_each_earlier_question_then_its_answer():
    data = replay(("hi", "hello"), ("my leave?", None), ("again?", "12 days"), ("now?", "x"))
    assert replay_turn.history_rows(data, 4) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "my leave?"},
        {"role": "user", "content": "again?"},
        {"role": "assistant", "content": "12 days"},
    ]
    assert replay_turn.history_rows(data, 1) == []


def test_history_leaves_out_approval_turns():
    data = replay(("apply leave", "Approve?"), (None, "Submitted"), ("status?", "pending"))
    assert [r["content"] for r in replay_turn.history_rows(data, 3)] == ["apply leave", "Approve?", "Submitted"]


def test_production_history_keeps_the_last_rows_then_trims():
    rows = [{"role": "user", "content": str(n)} for n in range(20)]
    trimmed = replay_turn.production_history(rows, 12, lambda kept: kept[-3:])
    assert [r["content"] for r in trimmed] == ["17", "18", "19"]
    seen = []
    replay_turn.production_history(rows, 12, lambda kept: seen.append(len(kept)) or kept)
    assert seen == [12]


def test_find_turn_refuses_missing_and_approval_turns():
    data = replay(("apply leave", "Approve?"), (None, "Submitted"))
    assert replay_turn.find_turn(data, 1)["user_query"] == "apply leave"
    with pytest.raises(replay_turn.ReplayError, match="No turn 5"):
        replay_turn.find_turn(data, 5)
    with pytest.raises(replay_turn.ReplayError, match="isn't supported"):
        replay_turn.find_turn(data, 2)


def test_summary_previews_each_run():
    record = {
        "turn": 2,
        "label": "before",
        "path": "replays/before-turn2.json",
        "runs": [
            {"outcome": "answer", "agent": "leave", "tool_calls": ["get_leave"], "answer": "You have\n0 days"},
            {"outcome": "error", "agent": None, "tool_calls": [], "error": "timeout"},
        ],
    }
    text = replay_turn.summary(record)
    assert "Turn 2 (before), 2 runs" in text
    assert "1. answer via leave (get_leave): You have 0 days" in text
    assert "2. error via ? (no tools): timeout" in text


def test_without_replay_input_it_says_only_chat_bugs_replay(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SF_WORKSPACE", str(tmp_path))
    assert replay_turn.main(["--worktree", str(tmp_path), "--turn", "1", "--label", "before"]) == 2
    assert "only chat bugs" in capsys.readouterr().err


def test_replay_input_from_a_cleaned_conversation():
    trace = json.loads((FIXTURES / "trace.json").read_text(encoding="utf-8"))
    second = copy.deepcopy(trace)
    second["id"] = "t2"
    second["timestamp"] = "2026-10-29T12:05:00.000Z"
    data = replay_input(clean_conversation("c-1", [trace, second]))

    assert data["conversation_id"] == "c-1"
    assert [t["turn"] for t in data["turns"]] == [1, 2]
    first = data["turns"][0]
    assert first["user_query"] == "what is my mobile number"
    assert first["answer"] and first["timestamp"] == trace["timestamp"]
    # No turn has issues in the fixture, so the guess is the last turn
    assert data["failing_turn"] == 2
