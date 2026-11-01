import os
import tempfile
from pathlib import Path

import pytest

# store reads this on import (app.py reaps runs then), so set it before any test module loads
os.environ["SF_WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="sf-tests-")

from software_factory import store  # noqa: E402


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A fresh workspace and database for every test."""
    root = tmp_path / "bug_workspace"
    monkeypatch.setattr(store, "WORKSPACE_DIR", root)
    monkeypatch.setattr(store, "DB_PATH", root / "factory.db")
    return root


def seed_issue(status: str, issue_id: str = "issue-1", identifier: str = "BEI-1", title: str = "Grade shows .None", **fields) -> Path:
    """Create an issue in status with a session, and return its workspace folder."""
    store.upsert_issue(issue_id, identifier, title)
    store.set_fields(issue_id, session_id="s", **fields)
    store.transition(issue_id, status)
    ws = store.issue_dir(issue_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws
