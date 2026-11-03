import importlib
import json
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


def test_screenshots_are_listed_and_counted(tmp_path):
    write(tmp_path, "verification.json", {"status": "passed", "browser": {"done": True, "passed": True}})
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "page.png").write_bytes(b"png")
    (tmp_path / "evidence" / "trace.zip").write_bytes(b"zip")
    assert evidence.screenshots(tmp_path) == ["page.png"]
    assert "1 screenshot on the plan page." in evidence.table(tmp_path)
    assert "screenshot" not in evidence.table(tmp_path, for_pr=False)


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
