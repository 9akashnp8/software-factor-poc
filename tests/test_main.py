import importlib
import json
import time
from pathlib import Path

import anyio
import httpx
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, RateLimitEvent, RateLimitInfo, ResultMessage

from conftest import git, seed_issue
from software_factory import config, store

main = importlib.import_module("software_factory.main")
FIXTURES = Path(__file__).parent / "fixtures"
CONVERSATION_ID = "0b6f2c1e-1111-4222-8333-444455556666"


def result(subtype="success", is_error=False, text="done") -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=10, duration_api_ms=10, is_error=is_error, num_turns=3, session_id="s2", total_cost_usd=0.5, result=text)


def fake_agent(monkeypatch, *, results=None, error=None, exit_plan=None, work=None):
    """Replace ClaudeSDKClient. exit_plan, if given, is sent to the session's callback as
    ExitPlanMode input; work, if given, is called as the agent's edits and commits."""
    sessions = []

    class FakeClient:
        def __init__(self, options):
            self.options = options
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            self.prompt = prompt

        async def receive_response(self):
            if exit_plan is not None:
                await self.options.can_use_tool("ExitPlanMode", exit_plan, None)
            if work is not None:
                work()
            if error:
                raise error
            for message in results if results is not None else [result()]:
                yield message

    monkeypatch.setattr(main, "ClaudeSDKClient", FakeClient)
    return sessions


def issue() -> dict:
    return store.get_issue("issue-1")


def bug(labels=("Bug",), description="Steps to reproduce"):
    return {
        "id": "issue-1",
        "identifier": "BEI-1",
        "title": "Wrong answer",
        "description": description,
        "url": "https://linear.app/x/BEI-1",
        "labels": {"nodes": [{"name": n} for n in labels]},
    }


@pytest.fixture
def triage(monkeypatch, tmp_path: Path):
    """Run triage for bug(...) with Linear, uploads and the worktree stubbed out."""
    downloads = []

    def fake_download(urls, dest, api_key):
        downloads.append(dest)
        return [dest / f"attachment_{i}.png" for i, _ in enumerate(urls)]

    monkeypatch.setattr(main, "download_uploads", fake_download)
    # Triage records the commit the plan is written on
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git(worktree, "init", "-q")
    git(worktree, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "base")
    monkeypatch.setattr(main, "ensure_worktree", lambda _: worktree)

    def run(bug_info):
        async def fake_get_issue(_):
            return bug_info

        monkeypatch.setattr(main, "get_issue", fake_get_issue)
        # As the worker does: queue it, then claim it, which hands out the slot
        store.enqueue_new(bug_info["id"], bug_info["identifier"])
        assert store.claim_next() == (bug_info["id"], "triage")
        anyio.run(main.triage_and_plan_issue, bug_info["id"])
        return downloads

    return run


# Plan-phase permissions


@pytest.fixture
def plan_callback(tmp_path: Path):
    ws = seed_issue(store.PLANNING)
    return ws, main.plan_phase_can_use_tool("issue-1", ws)


def decide(callback, tool, tool_input):
    return anyio.run(callback, tool, tool_input, None)


def test_plan_callback_allows_only_the_plan_write(plan_callback):
    _, callback = plan_callback
    assert isinstance(decide(callback, "Write", {"file_path": str(main.PLANS_DIR / "p.md")}), PermissionResultAllow)
    denied = decide(callback, "Write", {"file_path": str(Path.cwd() / "app.py")})
    assert isinstance(denied, PermissionResultDeny) and "read-only" in denied.message


def test_plan_callback_allows_reads_in_the_issue_workspace(plan_callback):
    ws, callback = plan_callback
    assert isinstance(decide(callback, "Read", {"file_path": str(ws / "attachments" / "a.png")}), PermissionResultAllow)
    assert isinstance(decide(callback, "Grep", {"pattern": "x", "path": str(ws)}), PermissionResultAllow)
    assert isinstance(decide(callback, "Read", {"file_path": str(ws.parent / "other" / "issue.md")}), PermissionResultDeny)


@pytest.mark.parametrize("tool", ["Bash", "PowerShell", "Edit", "WebFetch", "mcp__x__y"])
def test_plan_callback_denies_everything_else(plan_callback, tool):
    _, callback = plan_callback
    denied = decide(callback, tool, {"command": "echo hi > x"})
    assert isinstance(denied, PermissionResultDeny)
    assert tool in denied.message


def test_plan_callback_answers_questions_with_a_deny(plan_callback):
    _, callback = plan_callback
    assert decide(callback, "AskUserQuestion", {"questions": []}).message == main.NO_QUESTIONS


def test_exit_plan_mode_saves_plan_and_interrupts(plan_callback, tmp_path):
    ws, callback = plan_callback
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan")
    denied = decide(callback, "ExitPlanMode", {"planFilePath": str(plan_file)})
    assert denied.interrupt is True
    assert (ws / "plan.md").read_text() == "# Plan"
    assert issue()["status"] == store.PLAN_READY


def test_exit_plan_mode_without_a_plan_asks_again(plan_callback):
    _, callback = plan_callback
    denied = decide(callback, "ExitPlanMode", {})
    assert not denied.interrupt
    assert issue()["status"] == store.PLANNING


# Triage


def test_chat_bug_without_conversation_id_needs_attention(triage, monkeypatch):
    sessions = fake_agent(monkeypatch)
    triage(bug(labels=("Bug", "cpcgpt-chat")))
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "conversation_id" in issue()["needs_attention_reason"]
    assert sessions == []


def test_langfuse_failure_needs_attention(triage, monkeypatch):
    async def failing_fetch(*args):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(main, "fetch_conversation", failing_fetch)
    sessions = fake_agent(monkeypatch)
    triage(bug(labels=("Bug", "cpcgpt-chat"), description=f"{{{{conversation_id: {CONVERSATION_ID}}}}}"))
    assert issue()["needs_attention_reason"].startswith(f"Couldn't fetch conversation {CONVERSATION_ID}")
    assert sessions == []


def test_triage_gives_agent_reachable_context_and_saves_plan(triage, monkeypatch):
    trace = json.loads((FIXTURES / "trace.json").read_text(encoding="utf-8"))

    async def fake_fetch(*args):
        return {}, [trace]

    monkeypatch.setattr(main, "fetch_conversation", fake_fetch)
    sessions = fake_agent(monkeypatch, exit_plan={"plan": "# Plan"})
    description = f"{{{{conversation_id: {CONVERSATION_ID}}}}}\n![s](https://uploads.linear.app/a/b.png)"
    downloads = triage(bug(labels=("Bug", "cpcgpt-chat"), description=description))

    ws = store.issue_dir("issue-1")
    assert downloads == [ws / "attachments"] and ws.is_absolute()
    prompt = sessions[0].prompt
    assert str(ws / "attachments" / "attachment_0.png") in prompt
    assert str(ws / "conversation.json") in prompt
    assert json.loads((ws / "conversation.json").read_text(encoding="utf-8"))["session_id"] == CONVERSATION_ID
    assert issue()["status"] == store.PLAN_READY
    assert sessions[0].options.max_turns == main.LIMITS["plan"]["max_turns"]
    assert issue()["plan_base"] == git(main.ensure_worktree(None), "rev-parse", "HEAD")


def test_planner_ending_without_a_plan_needs_attention(triage, monkeypatch):
    fake_agent(monkeypatch, results=[result(text="Which database should I use?")])
    triage(bug())
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "Which database should I use?" in issue()["needs_attention_reason"]


# Implementation and rework


@pytest.fixture
def pr_calls(monkeypatch, tmp_path: Path) -> list:
    calls = []

    async def fake_open(issue_id):
        calls.append(issue_id)

    monkeypatch.setattr(main, "open_pull_request", fake_open)
    monkeypatch.setattr(main, "ensure_worktree", lambda _: tmp_path / "worktree")
    return calls


def implement():
    anyio.run(main.implement_and_review, "issue-1", True)


def test_implementation_hitting_turn_limit_needs_attention_and_skips_push(monkeypatch, pr_calls):
    ws = seed_issue(store.IMPLEMENTING)
    sessions = fake_agent(monkeypatch, results=[result("error_max_turns", is_error=True, text="")])
    implement()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "error_max_turns" in issue()["needs_attention_reason"]
    assert pr_calls == []
    assert not (ws / "report.md").exists()
    options = sessions[0].options
    assert (options.max_turns, options.resume, options.system_prompt) == (150, "s", main.AGENT_SYSTEM_PROMPT)


def test_successful_implementation_writes_report_and_opens_pr(monkeypatch, pr_calls):
    ws = seed_issue(store.IMPLEMENTING)
    fake_agent(monkeypatch, results=[result(text="Root cause: x")])
    implement()
    assert (ws / "report.md").read_text() == "Root cause: x"
    assert pr_calls == ["issue-1"]
    (run,) = store.runs("issue-1")
    assert (run["phase"], run["subtype"], run["cost_usd"]) == ("implement", "success", 0.5)
    assert issue()["session_id"] == "s2"


def test_crashed_run_needs_attention_and_closes_the_run(monkeypatch, pr_calls):
    seed_issue(store.IMPLEMENTING)
    fake_agent(monkeypatch, error=RuntimeError("CLI exited"))
    implement()
    assert issue()["status"] == store.NEEDS_ATTENTION
    assert "CLI exited" in issue()["needs_attention_reason"]
    (run,) = store.runs("issue-1")
    assert run["ended_at"] and "CLI exited" in run["error"]
    assert pr_calls == []


def test_rework_saves_new_plan_without_touching_report(monkeypatch, pr_calls):
    ws = seed_issue(store.REWORKING)
    (ws / "report.md").write_text("old report")
    sessions = fake_agent(monkeypatch, exit_plan={"plan": "# Revised"})
    anyio.run(main.implement_and_review, "issue-1", False, "Check the router")
    assert issue()["status"] == store.PLAN_READY
    assert (ws / "plan.md").read_text() == "# Revised"
    assert (ws / "report.md").read_text() == "old report"
    assert "Check the router" in sessions[0].prompt
    assert sessions[0].options.permission_mode == "plan"


# Refreshing a plan in review against the latest dev


@pytest.fixture
def refresh(dev_repo, monkeypatch):
    """An issue whose plan is being refreshed, its worktree, and a factory fix merged to dev since."""
    worktree = dev_repo.worktree()
    base = git(worktree, "rev-parse", "HEAD")
    ws = seed_issue(store.REFRESHING, plan_base=base)
    (ws / "plan.md").write_text("# Plan", encoding="utf-8")
    fix = seed_issue(store.DEPLOY_PENDING, issue_id="bei-17", identifier="BEI-17", slot=None, pr_id=5540)
    (fix / "qa.json").write_text(json.dumps({"root_cause": "No email check", "fix": "Emails are validated"}))
    monkeypatch.setattr(main, "ensure_worktree", lambda _: worktree)

    def run():
        anyio.run(main.refresh_plan, "issue-1")

    run.worktree, run.ws, run.base = worktree, ws, base
    run.merge = lambda: dev_repo.merge_pr(5540, "BEI-17: Email accepts test@123", ["apps/api/email.py"])
    return run


def last_event() -> str:
    return store.events("issue-1")[-1]["detail"]


def test_refresh_with_no_change_goes_back_to_review_with_the_reason(refresh, monkeypatch):
    new = refresh.merge()
    sessions = fake_agent(monkeypatch, results=[result(text="BEI-17 only touched email; the plan still holds.")])
    refresh()
    assert git(refresh.worktree, "rev-parse", "HEAD") == new
    assert (issue()["status"], issue()["plan_base"]) == (store.PLAN_READY, new)
    assert last_event() == (
        "Re-checked after 1 new merge to dev, including BEI-17: no change. "
        "BEI-17 only touched email; the plan still holds."
    )
    assert (refresh.ws / "plan.md").read_text(encoding="utf-8") == "# Plan"
    prompt, options = sessions[0].prompt, sessions[0].options
    assert "<dev_changes>" in prompt and "Merged PR 5540: BEI-17" in prompt and "Root cause: No email check" in prompt
    assert f"git diff {refresh.base[:10]}..HEAD" in prompt
    assert (options.permission_mode, options.resume, options.max_turns) == ("plan", "s", main.LIMITS["refresh"]["max_turns"])


def test_refresh_that_revises_the_plan_saves_it(refresh, monkeypatch):
    new = refresh.merge()
    fake_agent(monkeypatch, exit_plan={"plan": "# Revised"}, results=[result("error_during_execution", is_error=True, text="")])
    refresh()
    assert (issue()["status"], issue()["plan_base"]) == (store.PLAN_READY, new)
    assert (refresh.ws / "plan.md").read_text(encoding="utf-8") == "# Revised"
    assert last_event() == "Plan revised after 1 new merge to dev, including BEI-17"


def test_refresh_presenting_the_same_plan_counts_as_no_change(refresh, monkeypatch):
    refresh.merge()
    fake_agent(monkeypatch, exit_plan={"plan": "# Plan"}, results=[result("error_during_execution", is_error=True, text="")])
    refresh()
    assert issue()["status"] == store.PLAN_READY
    assert last_event() == "Re-checked after 1 new merge to dev, including BEI-17: no change"


def test_refresh_that_doesnt_finish_keeps_the_plan_and_its_base(refresh, monkeypatch):
    refresh.merge()
    fake_agent(monkeypatch, results=[result("error_max_turns", is_error=True, text="")])
    refresh()
    assert (issue()["status"], issue()["plan_base"]) == (store.PLAN_READY, refresh.base)
    assert last_event().startswith("The re-check didn't finish (error_max_turns")


def test_refresh_when_dev_hasnt_moved_runs_no_agent(refresh, monkeypatch):
    sessions = fake_agent(monkeypatch)
    refresh()
    assert sessions == []
    assert issue()["status"] == store.PLAN_READY
    assert last_event() == "dev hasn't moved since this plan; nothing to re-check"


def test_refresh_leaves_a_worktree_with_uncommitted_files_alone(refresh, monkeypatch):
    refresh.merge()
    (refresh.worktree / "scratch.txt").write_text("left by the planner")
    sessions = fake_agent(monkeypatch)
    refresh()
    assert sessions == []
    assert git(refresh.worktree, "rev-parse", "HEAD") == refresh.base
    assert issue()["status"] == store.PLAN_READY
    assert last_event() == "Couldn't update the worktree to the latest dev: uncommitted changes in the worktree: scratch.txt"


def test_refresh_refuses_a_branch_with_commits(refresh, monkeypatch):
    refresh.merge()
    git(refresh.worktree, "commit", "-q", "--allow-empty", "-m", "fix")
    sessions = fake_agent(monkeypatch)
    refresh()
    assert sessions == []
    assert last_event() == "Couldn't update the worktree to the latest dev: the branch already has commits"


# Rate limits, models and slot environments


def rejected(resets_at: float | None) -> RateLimitEvent:
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=resets_at), uuid="u", session_id="s")


def test_rate_limited_implementation_is_requeued_and_pauses_the_worker(monkeypatch, pr_calls):
    seed_issue(store.IMPLEMENTING)
    resets = int(time.time()) + 900
    fake_agent(monkeypatch, results=[rejected(resets), result("error_during_execution", is_error=True, text="")])
    implement()
    issue = store.get_issue("issue-1")
    assert (issue["status"], issue["attempts"]) == (store.IMPLEMENT_QUEUED, 1)
    assert store.paused_until() == resets
    assert store.runs("issue-1")[0]["rate_limited"] == 1
    assert pr_calls == []


def test_retried_implementation_is_told_to_continue(monkeypatch, pr_calls):
    seed_issue(store.IMPLEMENTING)
    store.requeue("issue-1", "rate limit")
    store.claim_next()
    sessions = fake_agent(monkeypatch)
    implement()
    assert sessions[0].prompt == main.CONTINUE_PROMPT


def test_api_429_without_an_event_still_requeues(monkeypatch, pr_calls):
    seed_issue(store.IMPLEMENTING)
    limited = result("success", is_error=True, text="")
    limited.api_error_status = 429
    fake_agent(monkeypatch, results=[limited])
    implement()
    assert store.get_issue("issue-1")["status"] == store.IMPLEMENT_QUEUED
    assert store.paused_until() is not None


def test_rejection_the_run_recovered_from_is_ignored(monkeypatch, pr_calls):
    seed_issue(store.IMPLEMENTING)
    fake_agent(monkeypatch, results=[rejected(time.time() + 900), result(text="Root cause: x")])
    implement()
    assert pr_calls == ["issue-1"]
    assert store.paused_until() is None


def test_rate_limited_triage_starts_over_from_the_queue(triage, monkeypatch):
    fake_agent(monkeypatch, results=[rejected(time.time() + 900), result("error_during_execution", is_error=True, text="")])
    triage(bug())
    assert store.get_issue("issue-1")["status"] == store.QUEUED


def test_agents_get_their_slot_env_and_phase_model(triage, monkeypatch, pr_calls):
    sessions = fake_agent(monkeypatch, exit_plan={"plan": "# Plan"})
    triage(bug())
    plan_options = sessions[0].options
    assert plan_options.model == config.PLAN_MODEL
    slot = store.get_issue("issue-1")["slot"]
    assert plan_options.env["API_PORT"] == str(config.SLOT_API_PORT_BASE + slot)
    assert plan_options.env["LINEAR_API_KEY"] == ""

    store.transition("issue-1", store.IMPLEMENTING)
    implement()
    assert sessions[1].options.model == config.IMPLEMENT_MODEL
    assert sessions[1].options.env == plan_options.env


def test_reopened_bug_plans_with_the_previous_attempt(triage, monkeypatch):
    ws = store.issue_dir("issue-1")
    (ws / main.PREVIOUS_ATTEMPT_DIR).mkdir(parents=True)
    (ws / main.PREVIOUS_ATTEMPT_DIR / "plan.md").write_text("# Old plan")
    sessions = fake_agent(monkeypatch, exit_plan={"plan": "# New plan"})
    triage(bug())
    assert "<previous_attempt>" in sessions[0].prompt and "# Old plan" in sessions[0].prompt


def test_implementation_keeps_its_report_and_qa_summary_apart(monkeypatch, pr_calls):
    ws = seed_issue(store.IMPLEMENTING)
    done = result(text='{"report": "..."}')
    done.structured_output = {"report": "Root cause: naive UTC times", "root_cause": " Times were read as local time. ", "fix": "Read as UTC."}
    sessions = fake_agent(monkeypatch, results=[done])
    implement()
    assert (ws / "report.md").read_text() == "Root cause: naive UTC times"
    assert json.loads((ws / "qa.json").read_text()) == {"root_cause": "Times were read as local time.", "fix": "Read as UTC."}
    assert sessions[0].options.output_format["schema"] == main.IMPLEMENTATION_SCHEMA
    assert "root_cause and fix" in sessions[0].prompt


def test_implementation_without_structured_output_still_reports(monkeypatch, pr_calls):
    ws = seed_issue(store.IMPLEMENTING)
    fake_agent(monkeypatch, results=[result(text="Root cause: x")])
    implement()
    assert (ws / "report.md").read_text() == "Root cause: x"
    assert not (ws / "qa.json").exists()
