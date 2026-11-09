import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from conftest import seed_issue
from software_factory import factory_log, store
from software_factory import run_transcript as transcripts

START = datetime(2026, 11, 6, 8, 20, 0, tzinfo=UTC)


def write(path: Path, *lines: tuple[int, dict]):
    """A transcript as factory_log.Transcript writes it, each line at START plus its seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for seconds, line in lines:
            at = (START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
            f.write(json.dumps({"at": at, **line}, ensure_ascii=False) + "\n")
    return path


def message(m) -> dict:
    return {"type": type(m).__name__, **factory_log._jsonable(m)}


def said(*blocks, parent=None) -> dict:
    return message(AssistantMessage(content=list(blocks), model="sonnet", parent_tool_use_id=parent))


def answered(tool_id, content, is_error=None) -> dict:
    return message(UserMessage(content=[ToolResultBlock(tool_id, content, is_error)]))


def head(phase="implement", prompt="Implement it") -> dict:
    return {"type": "run", "run_id": 1, "phase": phase, "model": "sonnet", "cwd": "/wt/BEI-1", "prompt": prompt, "resume": "abc12345"}


def done(**fields) -> dict:
    result = ResultMessage(
        subtype=fields.pop("subtype", "success"), duration_ms=521045, duration_api_ms=424015,
        is_error=fields.pop("is_error", False), num_turns=4, session_id="abc12345", total_cost_usd=3.0,
        usage={"output_tokens": 18724, "output_tokens_details": {"thinking_tokens": 3908}}, **fields,
    )
    return message(result)


def test_steps_put_the_agents_messages_between_folded_tool_calls(tmp_path):
    path = write(
        tmp_path / "1-implement.jsonl",
        (0, head()),
        (9, said(TextBlock("Starting with the test."))),
        (10, said(ToolUseBlock("t1", "Bash", {"command": "pytest -q", "description": "Run the tests"}))),
        (14, answered("t1", "1 failed")),
        (15, said(ToolUseBlock("t2", "Edit", {"file_path": "/wt/BEI-1/src/tools.py", "old_string": "a\nb\nc", "new_string": "a\nB\nc\nd"}))),
        (15, answered("t2", "updated")),
        (16, said(TextBlock("Fixed. **Committing** now."))),
        (17, said(ToolUseBlock("t3", "mcp__playwright__browser_click", {"element": "Your account menu"}))),
        (18, answered("t3", [{"type": "text", "text": "### Error\nInvalid arguments"}], is_error=True)),
        (19, said(ToolUseBlock("t4", "mcp__playwright__browser_navigate", {"url": "http://localhost:4305"}))),
        (20, answered("t4", "ok")),
        (21, done()),
        (22, {"type": "end", "outcome": "success: done", "rate_limited": False}),
    )
    t = transcripts.parse(path)

    assert [s["kind"] for s in t["steps"]] == ["text", "tools", "text", "tools"]
    first, second = t["steps"][1]["group"], t["steps"][3]["group"]
    assert t["steps"][0]["t"] == "+0:09"
    assert first.summary == "Ran 1 command, edited 1 file"
    assert first.titles == "Run the tests, src/tools.py"
    assert first.duration == "5.0s"
    edit = first.calls[1]
    assert (edit.added, edit.removed) == (2, 1)
    assert ("-", "b") in edit.diff and ("+", "B") in edit.diff and ("+", "d") in edit.diff
    assert second.summary == "Used the browser twice"
    assert second.titles == "Click “Your account menu”, Open http://localhost:4305"
    assert second.errors == 1 and second.calls[0].is_error and "Invalid arguments" in second.calls[0].output
    assert [s["anchor"] for s in t["outline"]] == ["m1", "m2"]
    assert t["tools"] == [("Browser", 2), ("Bash", 1), ("Edit", 1)]
    assert t["duration"] == "8m41" and t["api_duration"] == "7m04"
    assert t["usage"]["output"] == 18724 and t["usage"]["thinking"] == 3908
    assert len(t["events"]) == 13 and t["events"][0]["detail"] == "implement run on sonnet, resuming a session"


def test_a_subagents_messages_stay_inside_its_call(tmp_path):
    path = write(
        tmp_path / "1-plan.jsonl",
        (0, head("plan")),
        (1, said(ToolUseBlock("task", "Agent", {"description": "Find the grade code", "prompt": "Look"}))),
        (2, said(TextBlock("Looking in grades/"), parent="task")),
        (3, said(ToolUseBlock("g1", "Grep", {"pattern": "grade"}), parent="task")),
        (4, answered("g1", "grades/x.py")),
        (5, answered("task", "It's in grades/x.py")),
    )
    t = transcripts.parse(path)
    (step,) = t["steps"]
    (agent,) = step["group"].calls
    assert agent.kind == "agent" and agent.output == "It's in grades/x.py"
    assert [c["kind"] for c in agent.children] == ["text", "call"]
    assert t["outline"] == []


def test_a_saved_plan_ends_the_run_without_an_interruption_note(tmp_path):
    path = write(
        tmp_path / "1-plan.jsonl",
        (0, head("plan")),
        (5, said(ToolUseBlock("p1", "ExitPlanMode", {"plan": "# Plan\n\nFix it."}))),
        (6, answered("p1", "The user doesn't want to proceed with this tool use.", is_error=True)),
        (6, message(UserMessage(content=[TextBlock("[Request interrupted by user for tool use]")]))),
        (6, done(subtype="error_during_execution", is_error=True)),
    )
    t = transcripts.parse(path)
    (plan,) = t["steps"]
    assert plan["kind"] == "plan" and plan["stopped"] and plan["text"].startswith("# Plan")


def test_paths_read_relative_to_the_worktree_or_the_workspace(tmp_path):
    ws = tmp_path / "bug_workspace" / "issue-1"
    path = write(
        tmp_path / "1-implement.jsonl",
        (0, head()),
        (1, said(ToolUseBlock("r1", "Read", {"file_path": "/wt/BEI-1/a/b.py"}),
                 ToolUseBlock("r2", "Read", {"file_path": f"{ws}/evidence/shot.png"}))),
    )
    calls = transcripts.parse(path, ws)["calls"]
    assert [c.title for c in calls] == ["a/b.py", "workspace/evidence/shot.png"]
    assert transcripts.evidence_image(calls[1], ws, ["shot.png"]) == "shot.png"
    assert transcripts.evidence_image(calls[1], ws, []) is None


def test_a_long_output_is_cut_with_a_count_of_the_rest(tmp_path):
    path = write(
        tmp_path / "1-implement.jsonl",
        (0, head()),
        (1, said(ToolUseBlock("b1", "Bash", {"command": "cat big"}))),
        (2, answered("b1", "x" * (transcripts.OUTPUT_CHARS + 50))),
    )
    (call,) = transcripts.parse(path)["calls"]
    assert len(call.output) == transcripts.OUTPUT_CHARS and call.truncated == 50


def test_an_unreadable_line_is_kept_in_all_events(tmp_path):
    path = tmp_path / "1-plan.jsonl"
    path.write_text(json.dumps({"type": "run", "phase": "plan", "at": START.isoformat()}) + "\nnot json\n", encoding="utf-8")
    t = transcripts.parse(path)
    assert [e["type"] for e in t["events"]] == ["run", "unreadable"]


def test_wording_for_the_folded_line():
    def call(name, **inp):
        c = transcripts.Call(id=name + str(len(inp)), name=name, input=inp, at=START)
        transcripts._describe(c, [])
        return c

    calls = [call("Edit", file_path="a"), call("Edit", file_path="a"), call("Write", file_path="b"),
             call("Grep", pattern="x"), call("Skill", skill="review")]
    assert transcripts.summarize(calls) == "Edited 1 file, wrote 1 file, searched once, used Skill once"
    assert transcripts.span(0.84) == "0.8s" and transcripts.span(62) == "1m02" and transcripts.span(3900) == "1h05"
    assert transcripts.offset(3723) == "+1:02:03"


def finish(run_id, subtype, is_error):
    store.finish_run(run_id, result=type("R", (), {"subtype": subtype, "is_error": is_error, "session_id": "s",
                                                    "num_turns": 1, "total_cost_usd": 0.1, "duration_ms": 1, "result": ""})())


def test_a_planning_run_that_saved_its_plan_reads_as_plan_saved():
    seed_issue(store.PLANNING)
    run_id = store.start_run("issue-1", "plan", "worker")
    store.transition("issue-1", store.PLAN_READY)
    finish(run_id, "error_during_execution", True)
    (run,) = store.runs("issue-1")
    assert transcripts.outcome(run, store.events("issue-1")) == ("review", "plan saved")


def test_a_planning_run_that_stopped_without_a_plan_still_reads_as_an_error():
    seed_issue(store.PLANNING)
    run_id = store.start_run("issue-1", "plan", "worker")
    finish(run_id, "error_during_execution", True)
    (run,) = store.runs("issue-1")
    assert transcripts.outcome(run, store.events("issue-1")) == ("attn", "error during execution")


def test_an_implement_run_keeps_its_own_outcome():
    seed_issue(store.IMPLEMENTING)
    run_id = store.start_run("issue-1", "implement", "worker")
    assert transcripts.outcome(store.runs("issue-1")[0], []) == ("run", "running")
    finish(run_id, "success", False)
    assert transcripts.outcome(store.runs("issue-1")[0], []) == ("done", "success")
