import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from conftest import seed_issue
from software_factory import config, environment, store


def run(cwd: Path, *args: str):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def target(tmp_path: Path, monkeypatch) -> Path:
    """A target repo with an origin to fetch from and an untracked .env."""
    origin = tmp_path / "origin.git"
    run(tmp_path, "init", "--bare", "-b", config.BASE_BRANCH, str(origin))
    repo = tmp_path / "target"
    repo.mkdir()
    run(repo, "init", "-b", config.BASE_BRANCH)
    run(repo, "config", "user.email", "test@example.com")
    run(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("target")
    run(repo, "add", "README.md")
    run(repo, "commit", "-m", "base")
    run(repo, "remote", "add", "origin", str(origin))
    run(repo, "push", "-q", "origin", config.BASE_BRANCH)
    (repo / ".env").write_text("POSTGRES_USER=postgres\n")
    monkeypatch.setattr(config, "TARGET_REPO", repo)
    monkeypatch.setattr(config, "WORKTREE_DIR", tmp_path / "worktrees")
    return repo


@pytest.fixture
def provisioned(monkeypatch) -> list:
    calls = []

    def provision(worktree, env):
        calls.append((worktree, env))
        return []

    monkeypatch.setattr(environment, "provision", provision)
    return calls


def test_slot_env_isolates_ports_database_and_queue():
    env = environment.slot_env(3, "BEI-123")
    assert {k: env[k] for k in ("API_PORT", "WEB_PORT", "WEB_ORIGIN", "API_DATABASE_DB_NAME", "REDIS_DB")} == {
        "API_PORT": "8103",
        "WEB_PORT": "4303",
        "WEB_ORIGIN": "http://localhost:4303",
        "API_DATABASE_DB_NAME": "app_bei_123",
        "REDIS_DB": "4",
    }


def test_slot_env_blanks_the_factory_secrets(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "secret")
    env = environment.slot_env(1, "BEI-1")
    assert all(env[name] == "" for name in config.FACTORY_SECRETS)


def test_slot_env_serves_erp_fixtures_unless_turned_off(monkeypatch):
    assert environment.slot_env(1, "BEI-1")["CPC_ERP_MOCK_ENABLED"] == "true"
    monkeypatch.setattr(config, "SLOT_ERP_MOCK", False)
    assert "CPC_ERP_MOCK_ENABLED" not in environment.slot_env(1, "BEI-1")


def test_agent_env_points_at_the_workspace():
    seed_issue(store.PLANNING)
    env = environment.agent_env(store.get_issue("issue-1"))
    assert env["SF_WORKSPACE"] == str(store.issue_dir("issue-1"))
    worktree = str(config.WORKTREE_DIR / "BEI-1")
    assert env["SF_REPLAY"].startswith(f'uv run --all-packages --project "{worktree}" python "')
    assert env["SF_REPLAY"].endswith(f'replay_turn.py" --worktree "{worktree}"')


def test_agent_env_needs_a_slot():
    seed_issue(store.PLANNING, slot=None)
    with pytest.raises(environment.ProvisioningError, match="no slot"):
        environment.agent_env(store.get_issue("issue-1"))


def test_prepare_worktree_branches_copies_env_and_provisions_once(target, provisioned):
    seed_issue(store.GATHERING_CONTEXT, slot=2)
    issue = store.get_issue("issue-1")

    path = environment.prepare_worktree(issue)
    assert path == config.WORKTREE_DIR / "BEI-1"
    assert (path / "README.md").exists()
    assert (path / ".env").read_text() == "POSTGRES_USER=postgres\n"
    branch = subprocess.run(["git", "-C", str(path), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    assert branch == "fix/BEI-1"
    assert provisioned == [(path, environment.agent_env(issue))]

    environment.prepare_worktree(issue)
    assert len(provisioned) == 1


def test_prepare_worktree_replaces_a_leftover_directory_and_a_merged_branch(target, provisioned):
    # What a removal that failed part-way leaves: the untracked files, no .git, and the
    # issue's branch, already merged into the base
    run(target, "branch", "fix/BEI-1")
    leftover = config.WORKTREE_DIR / "BEI-1"
    (leftover / ".venv").mkdir(parents=True)
    (leftover / ".venv" / "pyvenv.cfg").write_text("home = nowhere")
    (leftover / ".env").write_text("STALE=1")
    seed_issue(store.GATHERING_CONTEXT, slot=2)

    path = environment.prepare_worktree(store.get_issue("issue-1"))

    assert (path / ".git").exists()
    assert (path / "README.md").exists()
    assert not (path / ".venv").exists()
    assert (path / ".env").read_text() == "POSTGRES_USER=postgres\n"


def test_prepare_worktree_keeps_an_earlier_attempts_unmerged_commits(target, provisioned):
    run(target, "switch", "-c", "fix/BEI-1")
    (target / "fix.txt").write_text("work in progress")
    run(target, "add", "fix.txt")
    run(target, "commit", "-m", "earlier attempt")
    run(target, "switch", config.BASE_BRANCH)
    seed_issue(store.GATHERING_CONTEXT, slot=2)

    path = environment.prepare_worktree(store.get_issue("issue-1"))

    assert (path / "fix.txt").read_text() == "work in progress"


def test_failed_provisioning_is_retried_next_time(target, monkeypatch):
    seed_issue(store.GATHERING_CONTEXT, slot=2)
    issue = store.get_issue("issue-1")

    def failing(worktree, env):
        raise environment.ProvisioningError("Postgres is down")

    monkeypatch.setattr(environment, "provision", failing)
    with pytest.raises(environment.ProvisioningError):
        environment.prepare_worktree(issue)

    monkeypatch.setattr(environment, "provision", lambda worktree, env: ["Seeding the policy collection failed: no MinIO"])
    environment.prepare_worktree(issue)
    assert store.events("issue-1")[-1]["detail"] == "Slot setup: Seeding the policy collection failed: no MinIO"


def test_provision_creates_migrates_seeds_and_installs(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd, **kwargs):
        calls.append((args[1:] if args[0].endswith(("npm", "npm.cmd")) else args, cwd))
        if args[-1] == "apps/api/seed.py":
            raise subprocess.CalledProcessError(1, args, stderr="MinIO unreachable")

    monkeypatch.setattr(environment.shutil, "which", lambda name: "C:/node/npm.cmd")
    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    problems = environment.provision(tmp_path, environment.slot_env(1, "BEI-1"))
    uv = ["uv", "run", "--all-packages"]
    assert calls == [
        ([*uv, "python", "apps/api/local_init.py"], tmp_path),
        ([*uv, "alembic", "upgrade", "head"], tmp_path / "apps" / "api"),
        ([*uv, "python", "apps/api/seed.py"], tmp_path),
        (["ci", "--no-audit", "--no-fund", "--prefer-offline"], tmp_path / "apps" / "web"),
    ]
    # Seeding and the packages don't stop the issue; the database does
    assert problems == ["Seeding the policy collection failed: MinIO unreachable"]


def test_provision_without_npm_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)
    monkeypatch.setattr(environment.subprocess, "run", lambda *a, **k: None)
    assert environment.provision(tmp_path, environment.slot_env(1, "BEI-1")) == [
        "Installing the web app's packages failed: npm isn't installed"
    ]


def test_provision_reports_the_script_error(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        # Workspace members hold the script's dependencies
        assert args[0][:3] == ["uv", "run", "--all-packages"]
        assert kwargs["env"]["API_DATABASE_DB_NAME"] == "app_bei_1"
        assert kwargs["env"]["UV_SYSTEM_CERTS"] == "1"
        assert "VIRTUAL_ENV" not in kwargs["env"]
        raise subprocess.CalledProcessError(1, args[0], stderr="pgvector is not installed")

    monkeypatch.setenv("VIRTUAL_ENV", "/factory/.venv")
    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    with pytest.raises(environment.ProvisioningError, match="local_init.py failed: pgvector is not installed"):
        environment.provision(tmp_path, environment.slot_env(1, "BEI-1"))


def test_stop_servers_kills_what_listens_on_the_slot_ports(monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(config, "SLOT_API_PORT_BASE", port - 3)
    server = subprocess.Popen(
        [sys.executable, "-c", f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)"]
    )
    try:
        for _ in range(50):
            if any(c.laddr.port == port for c in psutil.net_connections(kind="inet") if c.status == psutil.CONN_LISTEN):
                break
            time.sleep(0.1)
        # On Windows the venv's python.exe is a launcher; the listener is its child, and the
        # launcher exits with it, as uv's and npm's wrappers around a dev server do
        assert environment.stop_servers(3)
        assert server.wait(timeout=10) is not None
    finally:
        server.kill()


def test_stop_servers_with_nothing_running():
    assert environment.stop_servers(13) == []
