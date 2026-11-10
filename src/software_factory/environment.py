"""Each issue's worktree and its slot: ports, database and Redis queue of its own.

The variables are the ones the target repo reads for running several worktrees at once
(docs/agents/running-the-app.md there).
"""

import os
import re
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import psutil

from software_factory import config, store

# Two jobs adding worktrees to one repository at once can trip over git's locks
_worktree_lock = threading.Lock()
REPLAY_SCRIPT = Path(__file__).with_name("replay_turn.py")


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
    if config.SLOT_ERP_MOCK:
        # Overrides the worktree's .env, which pydantic-settings reads after the environment
        env["CPC_ERP_MOCK_ENABLED"] = "true"
    # Agents inherit the factory's environment; options.env entries override it
    env.update({name: "" for name in config.FACTORY_SECRETS})
    return env


def agent_env(issue) -> dict[str, str]:
    if issue["slot"] is None:
        raise ProvisioningError(f"{issue['identifier']} has no slot")
    return {
        **slot_env(issue["slot"], issue["identifier"]),
        # The workspace (reproduction.json, replays, evidence) is outside the worktree
        "SF_WORKSPACE": str(store.issue_dir(issue["id"])),
        "SF_REPLAY": replay_command(worktree_path(issue["identifier"])),
    }


def replay_command(worktree: Path) -> str:
    """$SF_REPLAY: the replay script, run in the worktree's environment rather than the factory's."""
    return (
        f'uv run --all-packages --project "{worktree}" python "{REPLAY_SCRIPT}" --worktree "{worktree}"'
    )


def stop_servers(slot: int) -> list[int]:
    """Stop whatever listens on the slot's api and web ports: servers an agent left running.

    They keep the ports taken and, on Windows, files in the worktree open, so it can't be
    removed. Kills each listener's whole process tree. Returns the pids stopped.
    """
    ports = {config.SLOT_API_PORT_BASE + slot, config.SLOT_WEB_PORT_BASE + slot}
    listeners = {
        c.pid
        for c in psutil.net_connections(kind="inet")
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in ports and c.pid
    }
    stopped = []
    for pid in listeners - {os.getpid()}:
        try:
            process = psutil.Process(pid)
            tree = [*process.children(recursive=True), process]
        except psutil.NoSuchProcess:
            continue
        for p in tree:
            try:
                p.kill()
                stopped.append(p.pid)
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(tree, timeout=10)
    return stopped


def _inside(path: str | None, root: str) -> bool:
    if not path:
        return False
    path = os.path.normcase(os.path.abspath(path))
    return path == root or path.startswith(root + os.sep)


def stop_worktree_processes(worktree: Path) -> list[int]:
    """Stop every process running from the worktree or in it, with its whole tree.

    For a teardown only: an issue still being worked runs its agent there too. stop_servers
    finds a dev server by its port, but an orphan of one doesn't listen: a uvicorn --reload
    worker whose reloader died holds the .venv's fastapi.exe open and listens on nothing,
    so on Windows the worktree can't be deleted. Returns the pids stopped.
    """
    root = os.path.normcase(os.path.abspath(worktree))
    tree = {}
    for process in psutil.process_iter(["pid", "exe", "cwd"]):
        if process.pid == os.getpid():
            continue
        if _inside(process.info["exe"], root) or _inside(process.info["cwd"], root):
            try:
                for p in [*process.children(recursive=True), process]:
                    tree.setdefault(p.pid, p)
            except psutil.NoSuchProcess:
                continue
    tree.pop(os.getpid(), None)
    stopped = []
    for p in tree.values():
        try:
            p.kill()
            stopped.append(p.pid)
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(list(tree.values()), timeout=10)
    return stopped


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(config.TARGET_REPO), *args], check=True, capture_output=True, text=True)


def force_rmtree(path: Path):
    """Delete a directory tree, read-only files (git objects, .venv) included."""

    def clear_readonly(func, failed_path, _exc):
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    shutil.rmtree(path, onexc=clear_readonly)


def _add_worktree(identifier: str, path: Path):
    """Branch from the remote tip so the fix starts on the latest base. Fetching leaves the
    main checkout's working tree and local branches untouched."""
    branch = branch_name(identifier)
    base = f"origin/{config.BASE_BRANCH}"
    _git("fetch", "origin", config.BASE_BRANCH)
    # Whatever an earlier worktree of this issue left must not be mistaken for a live one
    _git("worktree", "prune")
    if path.exists():
        # A removal that failed part-way (a locked .venv, say) leaves the untracked files
        # behind with no .git: git doesn't know the directory, and uv, finding no workspace
        # root in it, runs in the target repo's workspace that the directory sits inside.
        force_rmtree(path)
    if _git("branch", "--list", branch).stdout.strip() and _git("rev-list", "--count", f"{base}..{branch}").stdout.strip() != "0":
        # Commits of an earlier attempt that never merged: carry on from them
        _git("worktree", "add", str(path), branch)
    else:
        # A new issue, or a branch already merged: (re)start it at the base's tip
        _git("worktree", "add", "-B", branch, str(path), base)


def fetch_base():
    """Fetch origin's base branch into the target repo; its worktrees share the ref."""
    with _worktree_lock:
        _git("fetch", "origin", config.BASE_BRANCH)


def _setup_step(step: str, args: list[str], cwd: Path, env: dict[str, str]):
    try:
        subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=900)
    except subprocess.CalledProcessError as e:
        output = (e.stderr or e.stdout or "").strip()
        raise ProvisioningError(f"{step} failed: {output[-1500:]}") from e
    except subprocess.TimeoutExpired as e:
        raise ProvisioningError(f"{step} timed out") from e


def provision(worktree: Path, env: dict[str, str]) -> list[str]:
    """Set the slot up as the target's own commands would: its database, migrated and
    seeded, and the web app's packages.

    The database and its migrations are needed for anything to run, so their failure is
    raised. Seeding (policy questions) and the packages (the browser) are returned as
    problems instead: the issue can go on without them.
    """
    run_env = {**os.environ, **env}
    # The factory's virtualenv isn't the worktree's
    run_env.pop("VIRTUAL_ENV", None)
    # The target is a uv workspace: psycopg and the shared package belong to its members,
    # which a plain `uv run` at the root doesn't install (its bootstrap runs
    # `uv sync --all-packages`). This also gives the agent a complete .venv.
    uv = ["uv", "run", "--all-packages"]
    # `just init-local` and `just migrate`, with the slot's database name from env
    _setup_step("local_init.py", [*uv, "python", "apps/api/local_init.py"], worktree, run_env)
    _setup_step("Migrations", [*uv, "alembic", "upgrade", "head"], worktree / "apps" / "api", run_env)

    problems = []
    # `just seed`: the sample policies, ingested into the slot's own database
    optional = [("Seeding the policy collection", [*uv, "python", "apps/api/seed.py"], worktree)]
    # npm is a .cmd script on Windows; its full path lets subprocess start it
    if npm := shutil.which("npm"):
        args = [npm, "ci", "--no-audit", "--no-fund", "--prefer-offline"]
        optional.append(("Installing the web app's packages", args, worktree / "apps" / "web"))
    else:
        problems.append("Installing the web app's packages failed: npm isn't installed")
    for step, args, cwd in optional:
        try:
            _setup_step(step, args, cwd, run_env)
        except ProvisioningError as e:
            problems.append(str(e))
    return problems


def prepare_worktree(issue) -> Path:
    """The issue's worktree, created with its slot's database on first use."""
    identifier = issue["identifier"]
    path = worktree_path(identifier)
    with _worktree_lock:
        # A live worktree has a .git file; a directory without one is a leftover
        if not (path / ".git").exists():
            _add_worktree(identifier, path)

    marker = store.issue_dir(issue["id"]) / ".provisioned"
    if not marker.exists():
        # .env is untracked, so a new worktree has none
        source_env = config.TARGET_REPO / ".env"
        if source_env.exists() and not (path / ".env").exists():
            shutil.copyfile(source_env, path / ".env")
        for problem in provision(path, agent_env(issue)):
            store.add_event(issue["id"], f"Slot setup: {problem}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(database_name(identifier), encoding="utf-8")
    return path
