"""Each issue's worktree and its slot: ports, database and Redis queue of its own.

The variables are the ones the target repo reads for running several worktrees at once
(docs/agents/running-the-app.md there).
"""

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from software_factory import config, store

# Two jobs adding worktrees to one repository at once can trip over git's locks
_worktree_lock = threading.Lock()


class ProvisioningError(RuntimeError):
    pass


def database_name(identifier: str) -> str:
    return "app_" + re.sub(r"[^a-z0-9]+", "_", identifier.lower()).strip("_")


def worktree_path(identifier: str) -> Path:
    return config.WORKTREE_DIR / identifier


def branch_name(identifier: str) -> str:
    return f"fix/{identifier}"


def slot_env(slot: int, identifier: str) -> dict[str, str]:
    """The environment an issue's agents and provisioning run with."""
    web_port = config.SLOT_WEB_PORT_BASE + slot
    env = {
        "API_PORT": str(config.SLOT_API_PORT_BASE + slot),
        "WEB_PORT": str(web_port),
        "WEB_ORIGIN": f"http://localhost:{web_port}",
        "API_DATABASE_DB_NAME": database_name(identifier),
        "REDIS_DB": str(slot + 1),
        # uv's bundled certificates don't include the proxy's CA; the system store does
        "UV_SYSTEM_CERTS": os.environ.get("UV_SYSTEM_CERTS", "1"),
    }
    # Agents inherit the factory's environment; options.env entries override it
    env.update({name: "" for name in config.FACTORY_SECRETS})
    return env


def agent_env(issue) -> dict[str, str]:
    if issue["slot"] is None:
        raise ProvisioningError(f"{issue['identifier']} has no slot")
    return slot_env(issue["slot"], issue["identifier"])


def _git(*args: str):
    subprocess.run(["git", "-C", str(config.TARGET_REPO), *args], check=True, capture_output=True, text=True)


def provision(worktree: Path, env: dict[str, str]):
    """Create the slot's database with the target repo's own provisioning script."""
    run_env = {**os.environ, **env}
    # The factory's virtualenv isn't the worktree's
    run_env.pop("VIRTUAL_ENV", None)
    try:
        # The target is a uv workspace: psycopg and the shared package belong to its
        # members, which a plain `uv run` at the root doesn't install (its bootstrap
        # runs `uv sync --all-packages`). This also gives the agent a complete .venv.
        subprocess.run(
            ["uv", "run", "--all-packages", "python", "apps/api/local_init.py"],
            cwd=worktree,
            env=run_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.CalledProcessError as e:
        output = (e.stderr or e.stdout or "").strip()
        raise ProvisioningError(f"local_init.py failed: {output[-1500:]}") from e
    except subprocess.TimeoutExpired as e:
        raise ProvisioningError("local_init.py timed out") from e


def prepare_worktree(issue) -> Path:
    """The issue's worktree, created with its slot's database on first use."""
    identifier = issue["identifier"]
    path = worktree_path(identifier)
    with _worktree_lock:
        if not path.exists():
            # Branch from the remote tip so the fix starts on the latest base. Fetching
            # leaves the main checkout's working tree and local branches untouched.
            _git("fetch", "origin", config.BASE_BRANCH)
            _git("worktree", "add", "-b", branch_name(identifier), str(path), f"origin/{config.BASE_BRANCH}")

    marker = store.issue_dir(issue["id"]) / ".provisioned"
    if not marker.exists():
        # .env is untracked, so a new worktree has none
        source_env = config.TARGET_REPO / ".env"
        if source_env.exists() and not (path / ".env").exists():
            shutil.copyfile(source_env, path / ".env")
        provision(path, agent_env(issue))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(database_name(identifier), encoding="utf-8")
    return path
