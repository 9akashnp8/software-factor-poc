"""Before and after: what shows the bug happened and is gone, for the PR, the plan page and QA.

Built from the planner's reproduction.json, the implementer's verification.json (with the
factory's own re-run of the reproducing test) and the replays in replays/.

Screenshots are kept by the stage that took them: the planner's, from reproducing the bug, in
evidence/reproduction/, and those of the agents that fix and check it in evidence/verification/.
"""

import json
from datetime import datetime
from pathlib import Path

from software_factory import reproduction, store

VERIFICATION_FILE = "verification.json"
EVIDENCE_DIR = "evidence"
REPRODUCTION = "reproduction"
VERIFICATION = "verification"
STAGES = (REPRODUCTION, VERIFICATION)
REPLAYS_DIR = "replays"
SCREENSHOT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
# Table cells stay on one line and short; the full text is in the workspace
CELL_CHARS = 160

# How each reproduction method reads to QA, after "Reproduced:"
REPRODUCED_BY = {
    "test": "With an automated test, which failed before the fix.",
    "replay": "By replaying the conversation against the code before the fix.",
    "browser": "In the web app, before the fix.",
    "api": "By calling the api, before the fix.",
    "logs": "From the app's logs, before the fix.",
    "code": "By tracing the code, before the fix.",
    "manual": "By following the steps by hand, before the fix.",
}


def _json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verification(ws: Path) -> dict | None:
    return _json(ws / VERIFICATION_FILE)


def _cell(text) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > CELL_CHARS:
        text = text[: CELL_CHARS - 1] + "…"
    return text.replace("|", "\\|") or "—"


def _replay_cell(ws: Path, name: str | None) -> str:
    """A replay file as the share of runs per outcome and its first answer."""
    record = _json(ws / REPLAYS_DIR / Path(name).name) if name else None
    if not record or not record.get("runs"):
        return _cell(name)
    runs = record["runs"]
    answered = sum(1 for r in runs if r.get("outcome") == "answer")
    first = next((r.get("answer") or r.get("error") for r in runs), "")
    return _cell(f"{answered}/{len(runs)} answered; first: {first}")


def rows(ws: Path) -> list[tuple[str, str, str]]:
    """(check, before, after) rows; empty when nothing was verified."""
    repro = reproduction.load(ws) or {}
    checks = verification(ws)
    if checks is None:
        return []
    found = []
    covered = set()

    test = checks.get("test") or {}
    if test.get("command"):
        rerun = checks.get("rerun")
        after = test.get("after") or ""
        if rerun is not None:
            after = f"{after} (factory re-run: {'passed' if rerun.get('passed') else 'failed'})".strip()
        found.append((f"Test `{_cell(test['command'])}`", _cell(test.get("before")), _cell(after)))
        covered.add("test")

    replay = checks.get("replay") or {}
    if replay.get("before") or replay.get("after"):
        label = f"Replay of turn {replay['turn']}" if replay.get("turn") else "Replay"
        after = _replay_cell(ws, replay.get("after"))
        if replay.get("result"):
            after = _cell(f"{replay['result']} ({after})")
        found.append((label, _replay_cell(ws, replay.get("before")), after))
        covered.add("replay")

    browser = checks.get("browser") or {}
    if browser.get("done"):
        before = repro.get("evidence") if repro.get("method") == "browser" else None
        outcome = "passed" if browser.get("passed") else "failed"
        found.append(("Browser", _cell(before), _cell(f"{outcome}: {browser.get('result') or ''}")))
        covered.add("browser")

    method = repro.get("method")
    if repro.get("status") == reproduction.REPRODUCED and method and method not in covered:
        found.insert(0, (f"Reproduction ({method})", _cell(repro.get("evidence")), "—"))
    return found


def table(ws: Path, for_pr: bool = True) -> str | None:
    """The before/after table in Markdown, for the PR description, or for the plan page,
    which has its own heading and shows the screenshots."""
    checks = verification(ws)
    found = rows(ws)
    if checks is None or not found:
        return None
    lines = ["## Verification", ""] if for_pr else []
    lines += ["| Check | Before the fix | After the fix |", "|---|---|---|"]
    lines += [f"| {check} | {before} | {after} |" for check, before, after in found]
    status = checks.get("status", "unknown").replace("_", " ")
    lines += ["", f"Verification: **{status}**."]
    if for_pr:
        before, after = len(screenshots(ws, REPRODUCTION)), len(screenshots(ws, VERIFICATION))
        if before or after:
            lines.append(
                f"Screenshots on the plan page: {_count(before)} from reproducing the bug,"
                f" {_count(after)} from checking the fix."
            )
    return "\n".join(lines)


def _count(n: int) -> str:
    return str(n) if n else "none"


def stage_dir(ws: Path, stage: str) -> Path:
    """Where a stage's browser saves its screenshots."""
    if stage not in STAGES:
        raise ValueError(f"unknown evidence stage {stage!r}")
    return ws / EVIDENCE_DIR / stage


def _images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SCREENSHOT_SUFFIXES]


def _approved_at(ws: Path) -> float | None:
    """When the plan was last approved, the line between the planner's screenshots and the
    implementer's; None when it never was."""
    approvals = [e["at"] for e in store.events(ws.name) if e["to_status"] == store.IMPLEMENT_QUEUED]
    return datetime.fromisoformat(approvals[-1]).timestamp() if approvals else None


def _legacy(ws: Path, stage: str) -> list[Path]:
    """A stage's share of the one flat evidence/ folder workspaces had before the split.

    With nothing verified yet, all of it is the planner's. Once something is, what was saved
    after the plan's approval is the implementer's; with no approval to tell them apart,
    nothing counts as verification.
    """
    files = _images(ws / EVIDENCE_DIR)
    if not files:
        return []
    approved = _approved_at(ws) if verification(ws) is not None else None

    def stage_of(p: Path) -> str:
        return VERIFICATION if approved is not None and p.stat().st_mtime >= approved else REPRODUCTION

    return [p for p in files if stage_of(p) == stage]


def _shots(ws: Path, stage: str) -> dict[str, Path]:
    """A stage's screenshots by file name, oldest first."""
    files = sorted(_images(stage_dir(ws, stage)) + _legacy(ws, stage), key=lambda p: p.stat().st_mtime)
    found: dict[str, Path] = {}
    for path in files:
        found.setdefault(path.name, path)
    return found


def screenshots(ws: Path, stage: str) -> list[str]:
    """File names of the screenshots a stage took, oldest first."""
    return list(_shots(ws, stage))


def screenshot_path(ws: Path, stage: str, name: str) -> Path | None:
    """The file behind a screenshot that screenshots() lists; None for anything else, so no
    request can reach a file outside the evidence folders."""
    if stage not in STAGES:
        return None
    return _shots(ws, stage).get(name)


def reproduced_sentence(ws: Path) -> str | None:
    """How the bug was reproduced, in plain words for QA; None when it wasn't."""
    repro = reproduction.load(ws)
    if not repro or repro.get("status") != reproduction.REPRODUCED:
        return None
    return REPRODUCED_BY.get(repro.get("method"), "Before the fix.")
