import os
import tempfile
from pathlib import Path

import pytest

# store reads this on import, so set it before any test module loads
os.environ["SF_WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="sf-tests-")

from software_factory import config, store  # noqa: E402


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """A fresh workspace and database for every test, and no writes to the real Linear."""
    root = tmp_path / "bug_workspace"
    monkeypatch.setattr(store, "WORKSPACE_DIR", root)
    monkeypatch.setattr(store, "DB_PATH", root / "factory.db")
    monkeypatch.setattr(config, "LINEAR_WRITEBACK", False)
    # Settings from your .env mustn't change what the tests expect
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    return root


def set_slot(issue_id: str, slot: int | None):
    # Only claim_next() hands out slots in the factory itself
    with store._db() as conn:
        conn.execute("UPDATE issues SET slot = ? WHERE id = ?", (slot, issue_id))


def seed_issue(status: str, issue_id: str = "issue-1", identifier: str = "BEI-1", title: str = "Grade shows .None", slot: int | None = 1, **fields) -> Path:
    """Create an issue in status with a session and a slot, and return its workspace folder."""
    store.upsert_issue(issue_id, identifier, title)
    store.set_fields(issue_id, session_id="s", **fields)
    store.transition(issue_id, status)
    set_slot(issue_id, slot)
    ws = store.issue_dir(issue_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws
