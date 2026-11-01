import subprocess
from pathlib import Path

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
    monkeypatch.setattr(environment, "provision", lambda worktree, env: calls.append((worktree, env)))
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
    assert provisioned == [(path, environment.slot_env(2, "BEI-1"))]

    environment.prepare_worktree(issue)
    assert len(provisioned) == 1


def test_failed_provisioning_is_retried_next_time(target, monkeypatch):
    seed_issue(store.GATHERING_CONTEXT, slot=2)
    issue = store.get_issue("issue-1")

    def failing(worktree, env):
        raise environment.ProvisioningError("Postgres is down")

    monkeypatch.setattr(environment, "provision", failing)
    with pytest.raises(environment.ProvisioningError):
        environment.prepare_worktree(issue)

    calls = []
    monkeypatch.setattr(environment, "provision", lambda worktree, env: calls.append(worktree))
    environment.prepare_worktree(issue)
    assert calls == [config.WORKTREE_DIR / "BEI-1"]


def test_provision_reports_the_script_error(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs["env"]["API_DATABASE_DB_NAME"] == "app_bei_1"
        assert "VIRTUAL_ENV" not in kwargs["env"]
        raise subprocess.CalledProcessError(1, args[0], stderr="pgvector is not installed")

    monkeypatch.setenv("VIRTUAL_ENV", "/factory/.venv")
    monkeypatch.setattr(environment.subprocess, "run", fake_run)
    with pytest.raises(environment.ProvisioningError, match="pgvector is not installed"):
        environment.provision(tmp_path, environment.slot_env(1, "BEI-1"))
