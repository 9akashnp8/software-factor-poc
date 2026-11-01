"""Settings, read from the environment once at import. Defaults are this machine's setup."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


# The repo the factory fixes, and where its per-issue worktrees go
TARGET_REPO = Path(os.getenv("SF_TARGET_REPO", "C:/Users/ar14152/code/cpc-agentic-workplace-platform"))
WORKTREE_DIR = Path(os.getenv("SF_WORKTREE_DIR", str(TARGET_REPO / ".claude" / "worktrees")))
BASE_BRANCH = os.getenv("SF_BASE_BRANCH", "dev")

LANGFUSE_URL = os.getenv("SF_LANGFUSE_URL", "http://10.60.227.122:3000")

# Azure DevOps Server repo behind the target's `origin`
ADO_REPO_URL = os.getenv(
    "SF_ADO_REPO_URL",
    "https://devops.cpc.gov.ae/tfs/Applications/CPC-DataLakehousePlatform/_git/agentic-workplace-platform",
)
ADO_REPO_API = os.getenv(
    "SF_ADO_REPO_API",
    "https://devops.cpc.gov.ae/tfs/Applications/CPC-DataLakehousePlatform/_apis/git/repositories/agentic-workplace-platform",
)
ADO_API_VERSION = "6.0"


def ado_pat() -> str:
    """Read when needed rather than at import, so a missing PAT fails only what needs it."""
    if not (pat := os.getenv("AZURE_DEVOPS_PAT")):
        raise RuntimeError("AZURE_DEVOPS_PAT is not set")
    return pat


# Each model keeps its own prompt cache, but the human review between planning and
# implementation outlasts the cache anyway, so the switch costs nothing extra
PLAN_MODEL = os.getenv("SF_PLAN_MODEL", "opus")
IMPLEMENT_MODEL = os.getenv("SF_IMPLEMENT_MODEL", "sonnet")
REVIEW_MODEL = os.getenv("SF_REVIEW_MODEL", "sonnet")

MAX_CONCURRENT_RUNS = _int("SF_MAX_CONCURRENT_RUNS", 2)
INGEST_SECONDS = _int("SF_INGEST_SECONDS", 300)
MERGE_POLL_SECONDS = _int("SF_MERGE_POLL_SECONDS", 600)
# Rate-limited runs go back on the queue this many times before needing attention
MAX_ATTEMPTS = _int("SF_MAX_ATTEMPTS", 3)

# The web app's address, for the plan link in PR descriptions; no link when unset
PUBLIC_URL = os.getenv("SF_PUBLIC_URL", "").rstrip("/")

LINEAR_TODO_STATE = os.getenv("SF_LINEAR_TODO_STATE", "Todo")
LINEAR_BUG_LABEL = os.getenv("SF_LINEAR_BUG_LABEL", "Bug")
LINEAR_IN_PROGRESS_STATE = os.getenv("SF_LINEAR_IN_PROGRESS_STATE", "In Progress")
LINEAR_MERGED_STATE = os.getenv("SF_LINEAR_MERGED_STATE", "In Review")
LINEAR_WRITEBACK = os.getenv("SF_LINEAR_WRITEBACK", "1") != "0"

# Slot n gets API 8100+n, web 4300+n and Redis DB n+1. Redis has 16 DBs and the target
# uses 0 (Langfuse) and 1 (the main checkout's queue), which leaves 14 slots.
SLOT_COUNT = _int("SF_SLOT_COUNT", 14)
SLOT_API_PORT_BASE = _int("SF_SLOT_API_PORT_BASE", 8100)
SLOT_WEB_PORT_BASE = _int("SF_SLOT_WEB_PORT_BASE", 4300)

# The factory's own credentials. Agents inherit the factory's environment, so these are
# blanked in every agent's env.
FACTORY_SECRETS = (
    "LINEAR_API_KEY",
    "LANGFUSE_ENCODED_TOKEN",
    "AZURE_DEVOPS_PAT",
    "SF_USERS",
    "FLASK_SECRET_KEY",
)
