import importlib
import json
import os
from datetime import datetime
from pathlib import Path

from conftest import seed_issue
from software_factory import deployments, evidence, store

main = importlib.import_module("software_factory.main")

REPRODUCED = {"status": "reproduced", "method": "test", "steps": ["pytest -k grade"], "evidence": "AssertionError: '.None'"}


def write(ws: Path, name: str, data: dict):
    path = ws / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def replay_file(ws: Path, name: str, *answers: str):
    write(ws, f"replays/{name}", {"runs": [{"outcome": "answer", "answer": a} for a in answers]})


def test_nothing_verified_means_no_table(tmp_path):
    write(tmp_path, "reproduction.json", REPRODUCED)
    assert evidence.table(tmp_path) is None


def test_test_row_shows_before_after_and_the_factory_rerun(tmp_path):
    write(tmp_path, "reproduction.json", REPRODUCED)
    write(tmp_path, "verification.json", {
        "status": "passed",
        "test": {"command": "uv run pytest -k grade", "before": "1 failed | AssertionError", "after": "1 passed"},
        "rerun": {"command": "uv run pytest -k grade", "passed": True, "output": "1 passed"},
    })
    text = evidence.table(tmp_path)
    assert text.startswith("## Verification\n\n| Check | Before the fix | After the fix |")
    assert "| Test `uv run pytest -k grade` | 1 failed \\| AssertionError | 1 passed (factory re-run: passed) |" in text
    assert text.endswith("Verification: **passed**.")
    # The test row covers the reproduction, so it isn't repeated
    assert "Reproduction (test)" not in text
    assert not evidence.table(tmp_path, for_pr=False).startswith("## ")


def test_replay_row_counts_answers_from_the_replay_files(tmp_path):
    write(tmp_path, "reproduction.json", {**REPRODUCED, "method": "replay"})
    replay_file(tmp_path, "before-turn2.json", "You have 0 days", "You have 0 days", "You have 0 days")
    replay_file(tmp_path, "after-turn2.json", "You have 12 days", "You have 12 days", "You have 12 days")
    write(tmp_path, "verification.json", {
        "status": "passed",
        "replay": {"turn": 2, "before": "replays/before-turn2.json", "after": "after-turn2.json", "result": "Balance is right in all runs"},
    })
    (row,) = evidence.rows(tmp_path)
    assert row == (
        "Replay of turn 2",
        "3/3 answered; first: You have 0 days",
        "Balance is right in all runs (3/3 answered; first: You have 12 days)",
    )


def test_browser_row_and_a_reproduction_the_checks_dont_cover(tmp_path):
    write(tmp_path, "reproduction.json", {**REPRODUCED, "method": "api", "evidence": "GET /leave returned 500"})
    write(tmp_path, "verification.json", {
        "status": "passed",
        "browser": {"done": True, "passed": True, "result": "The balance shows 12 days", "steps": []},
    })
    assert evidence.rows(tmp_path) == [
        ("Reproduction (api)", "GET /leave returned 500", "—"),
        ("Browser", "—", "passed: The balance shows 12 days"),
    ]


def test_long_cells_are_cut_to_one_line(tmp_path):
    write(tmp_path, "verification.json", {"status": "failed", "test": {"command": "x", "before": "a\n" * 200, "after": ""}})
    (_, before, after) = evidence.rows(tmp_path)[0]
    assert "\n" not in before and len(before) == evidence.CELL_CHARS and before.endswith("…")
    assert after == "—"
    assert evidence.table(tmp_path).endswith("Verification: **failed**.")


def shot(ws: Path, folder: str, name: str, at: float | None = None) -> Path:
    """A screenshot in evidence/<folder>/ ("" for the flat folder of older workspaces)."""
    path = ws / "evidence" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    if at is not None:
        os.utime(path, (at, at))
    return path


VERIFIED = {"status": "passed", "browser": {"done": True, "passed": True}}


def test_screenshots_are_kept_by_the_stage_that_took_them(tmp_path):
    write(tmp_path, "verification.json", VERIFIED)
    shot(tmp_path, "reproduction", "before.png")
    shot(tmp_path, "verification", "after.png")
    (tmp_path / "evidence" / "verification" / "trace.zip").write_bytes(b"zip")
    assert evidence.screenshots(tmp_path, evidence.REPRODUCTION) == ["before.png"]
    assert evidence.screenshots(tmp_path, evidence.VERIFICATION) == ["after.png"]
    assert "Screenshots on the plan page: 1 from reproducing the bug, 1 from checking the fix." in evidence.table(tmp_path)
    assert "Screenshots" not in evidence.table(tmp_path, for_pr=False)


def test_the_pr_never_counts_the_planners_screenshots_as_checking_the_fix(tmp_path):
    write(tmp_path, "verification.json", VERIFIED)
    shot(tmp_path, "reproduction", "a.png")
    shot(tmp_path, "reproduction", "b.png")
    assert evidence.screenshots(tmp_path, evidence.VERIFICATION) == []
    assert "Screenshots on the plan page: 2 from reproducing the bug, none from checking the fix." in evidence.table(tmp_path)


def test_a_flat_folder_from_before_the_split_is_reproduction_until_something_is_verified():
    ws = seed_issue(store.PLAN_READY)
    shot(ws, "", "repro.png")
    assert evidence.screenshots(ws, evidence.REPRODUCTION) == ["repro.png"]
    assert evidence.screenshots(ws, evidence.VERIFICATION) == []


def test_a_flat_folder_splits_at_the_plans_approval():
    ws = seed_issue(store.PLAN_READY)
    store.transition("issue-1", store.IMPLEMENT_QUEUED)
    approved = datetime.fromisoformat(store.events("issue-1")[-1]["at"]).timestamp()
    shot(ws, "", "repro.png", at=approved - 600)
    shot(ws, "", "after.png", at=approved + 600)
    write(ws, "verification.json", VERIFIED)
    assert evidence.screenshots(ws, evidence.REPRODUCTION) == ["repro.png"]
    assert evidence.screenshots(ws, evidence.VERIFICATION) == ["after.png"]


def test_a_flat_folder_with_no_approval_to_split_at_counts_nothing_as_verification(tmp_path):
    write(tmp_path, "verification.json", VERIFIED)
    shot(tmp_path, "", "shot.png")
    assert evidence.screenshots(tmp_path, evidence.VERIFICATION) == []
    assert evidence.screenshots(tmp_path, evidence.REPRODUCTION) == ["shot.png"]


def test_screenshot_path_returns_only_listed_files(tmp_path):
    listed = shot(tmp_path, "reproduction", "a.png")
    (tmp_path / "secret.png").write_bytes(b"png")
    assert evidence.screenshot_path(tmp_path, evidence.REPRODUCTION, "a.png") == listed
    assert evidence.screenshot_path(tmp_path, evidence.VERIFICATION, "a.png") is None
    assert evidence.screenshot_path(tmp_path, evidence.REPRODUCTION, "../../secret.png") is None
    assert evidence.screenshot_path(tmp_path, "..", "secret.png") is None
    assert evidence.screenshot_path(tmp_path, "", "a.png") is None


def test_reproduced_sentence_for_qa(tmp_path):
    assert evidence.reproduced_sentence(tmp_path) is None
    write(tmp_path, "reproduction.json", {**REPRODUCED, "method": "replay"})
    assert evidence.reproduced_sentence(tmp_path) == "By replaying the conversation against the code before the fix."
    write(tmp_path, "reproduction.json", {"status": "not_attempted", "reason": "needs real ERP data"})
    assert evidence.reproduced_sentence(tmp_path) is None


def test_pr_description_puts_the_evidence_before_the_report():
    ws = seed_issue(store.IMPLEMENTING)
    (ws / "report.md").write_text("x" * 5000)
    write(ws, "verification.json", {"status": "passed", "test": {"command": "pytest -k grade", "before": "1 failed", "after": "1 passed"}})
    description = main.pr_description(ws, "https://linear.app/x/BEI-1")
    assert description.startswith("Linear issue: https://linear.app/x/BEI-1\n\n---\n\n## Verification")
    assert "| Test `pytest -k grade` | 1 failed | 1 passed |" in description
    assert len(description) <= main.ADO_DESCRIPTION_LIMIT


def test_qa_comment_says_how_it_was_reproduced_and_checked():
    ws = seed_issue(store.DEPLOYED, pr_id=7, pr_url="https://ado/pullrequest/7")
    write(ws, "qa.json", {"root_cause": "Why.", "fix": "What.", "checked": "Opened the page as jane@cpc.gov.ae; it shows 12 days."})
    write(ws, "reproduction.json", REPRODUCED)
    body = deployments.comment_body(store.get_issue("issue-1"))
    assert body.endswith(
        "**Fix:** What.\n\n"
        "**Reproduced:** With an automated test, which failed before the fix.\n\n"
        "**Checked:** Opened the page as <email>; it shows 12 days."
    )
