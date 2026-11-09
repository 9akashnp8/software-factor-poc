import importlib
import json
import logging
import subprocess

import anyio
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

from conftest import seed_issue
from software_factory import config, factory_log, store
from test_app import AUTH, client  # noqa: F401  (client is a fixture)
from test_main import fake_agent, result

main = importlib.import_module("software_factory.main")
worker = importlib.import_module("software_factory.worker")


def lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def log_text(path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


# The log file


def test_setup_writes_a_file_tagged_with_the_issue(workspace):
    path = factory_log.setup("worker", console=False)
    assert path == workspace / "factory-logs" / "worker.log"
    with factory_log.issue_context("BEI-7", "plan"):
        logging.getLogger("software_factory.main").info("planning")
    logging.getLogger("software_factory.worker").info("idle")
    text = log_text(path)
    assert "[BEI-7 plan] planning" in text
    assert "[-] idle" in text


def test_the_log_folder_can_be_moved(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_LOG_DIR", str(tmp_path / "elsewhere"))
    assert factory_log.setup("cli", console=False) == tmp_path / "elsewhere" / "cli.log"


def test_setup_twice_adds_no_second_handler():
    factory_log.setup("worker", console=False)
    count = len(logging.getLogger().handlers)
    factory_log.setup("worker", console=False)
    assert len(logging.getLogger().handlers) == count


def test_factory_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "pat-1234567890")
    path = factory_log.setup("worker", console=False)
    logging.getLogger("software_factory").error("push to https://x:pat-1234567890@host failed")
    text = log_text(path)
    assert "pat-1234567890" not in text
    assert "https://x:<secret>@host" in text


def test_short_secret_values_are_left_alone(monkeypatch):
    # A one-letter "secret" would mask every word with that letter in it
    monkeypatch.setenv("LOKI_ORG_ID", "a")
    assert factory_log.mask_secrets("a cat") == "a cat"


def test_the_web_apps_polls_stay_out_of_the_file():
    path = factory_log.setup("web", console=False)
    requests = logging.getLogger("werkzeug")
    requests.info('127.0.0.1 - - [x] "GET /api/attention HTTP/1.1" 200 -')
    requests.info('127.0.0.1 - - [x] "GET /static/factory.css HTTP/1.1" 304 -')
    requests.info('127.0.0.1 - - [x] "POST /issue/i/approve HTTP/1.1" 302 -')
    text = log_text(path)
    assert "/api/attention" not in text and "/static/" not in text
    assert "/approve" in text


def test_describe_includes_a_failed_commands_output():
    error = subprocess.CalledProcessError(1, ["git", "push"], stderr="! [rejected] fix/BEI-1 (fetch first)\n")
    assert "[rejected]" in factory_log.describe(error)
    assert factory_log.describe(ValueError("x")) == "ValueError('x')"


# Status changes


def test_status_changes_and_events_are_logged(caplog):
    seed_issue(store.PLANNING)
    with caplog.at_level(logging.INFO, logger="software_factory.store"):
        store.transition("issue-1", store.NEEDS_ATTENTION, reason="Planning ended without a plan")
        store.add_event("issue-1", "Linear label added", actor="akash")
    attention = next(r for r in caplog.records if "needs-attention" in r.getMessage())
    assert attention.levelno == logging.WARNING
    assert attention.getMessage() == f"BEI-1: {store.PLANNING} -> needs-attention: Planning ended without a plan"
    assert "BEI-1: Linear label added (by akash)" in caplog.text


# Agent run transcripts


def run(phase="plan", prompt="Plan BEI-1"):
    issue = store.get_issue("issue-1")
    options = main.planning_options(issue, store.issue_dir("issue-1"), store.issue_dir("issue-1"))
    return anyio.run(main.run_agent, "issue-1", phase, options, prompt, "worker")


def transcript(phase="plan"):
    (path,) = (store.issue_dir("issue-1") / factory_log.TRANSCRIPTS_DIR).glob(f"*-{phase}.jsonl")
    return lines(path)


def test_a_run_keeps_every_message_in_its_transcript(monkeypatch):
    seed_issue(store.PLANNING)
    said = AssistantMessage(
        content=[TextBlock("Reading the grade code"), ToolUseBlock("t1", "Read", {"file_path": "grade.py"})],
        model="opus",
    )
    fake_agent(monkeypatch, results=[said, result()])
    run()
    head, message, done, end = transcript()
    assert head["type"] == "run" and head["phase"] == "plan" and head["prompt"] == "Plan BEI-1"
    assert head["model"] == config.PLAN_MODEL
    assert message["type"] == "AssistantMessage"
    assert message["content"][1] == {"id": "t1", "name": "Read", "input": {"file_path": "grade.py"}}
    assert done["type"] == "ResultMessage" and done["total_cost_usd"] == 0.5
    assert end == {**end, "type": "end", "outcome": "success: done", "rate_limited": False}


def test_a_crashed_run_keeps_its_traceback(monkeypatch, caplog):
    seed_issue(store.PLANNING)
    fake_agent(monkeypatch, error=RuntimeError("CLI exited with 1"))
    assert run() is None
    error = next(line for line in transcript() if line["type"] == "error")
    assert "CLI exited with 1" in error["error"]
    assert "Traceback" in error["traceback"]
    assert "plan run 1 crashed" in caplog.text


def test_a_run_goes_on_when_its_transcript_cant_be_written(monkeypatch, tmp_path, caplog):
    seed_issue(store.PLANNING)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    monkeypatch.setattr(factory_log, "transcript_path", lambda *_: blocker / "1-plan.jsonl")
    fake_agent(monkeypatch)
    assert run().subtype == "success"
    assert "Can't write the transcript" in caplog.text


def test_the_clis_stderr_goes_to_the_transcript(monkeypatch):
    seed_issue(store.PLANNING)
    # The SDK calls the callback with each line the CLI writes to stderr, during the run
    sessions = fake_agent(monkeypatch, work=lambda: sessions[0].options.stderr("MCP server playwright failed to start\n"))
    run()
    assert {"type": "stderr", "line": "MCP server playwright failed to start"} in [
        {k: v for k, v in line.items() if k != "at"} for line in transcript()
    ]


def test_the_page_links_each_runs_transcript(monkeypatch, client):  # noqa: F811
    seed_issue(store.PLANNING)
    fake_agent(monkeypatch)
    run()
    http = client
    page = http.get("/issue/issue-1/plan", headers=AUTH).get_data(as_text=True)
    assert "/issue/issue-1/runs/1/transcript" in page
    shown = http.get("/issue/issue-1/runs/1/transcript", headers=AUTH)
    assert shown.status_code == 200 and shown.mimetype == "text/html" and "Plan run" in shown.get_data(as_text=True)
    served = http.get("/issue/issue-1/runs/1/transcript.jsonl", headers=AUTH)
    assert served.status_code == 200 and served.mimetype == "text/plain"
    assert json.loads(served.get_data(as_text=True).splitlines()[0])["type"] == "run"
    assert http.get("/issue/issue-1/runs/2/transcript", headers=AUTH).status_code == 404


# The worker


def test_a_failed_job_is_logged_with_its_issue(workspace):
    path = factory_log.setup("worker", console=False)
    seed_issue(store.QUEUED)

    async def failing(issue_id, job):
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="remote rejected")

    async def go():
        dispatcher = worker.Dispatcher(1, failing)
        await dispatcher._run("issue-1", "triage")

    anyio.run(go)
    text = log_text(path)
    assert "[BEI-1 triage] triage job started (queued)" in text
    assert "[BEI-1 triage] triage job failed" in text and "remote rejected" in text
    assert "Traceback" in text
    assert "triage job ended after 0s: needs-attention" in text
