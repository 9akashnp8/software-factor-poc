"""The target's api logs from Loki, as context for the agents.

Triage fetches the window around the bug into the workspace's logs/; agents ask for other
windows through the factory's query_logs tool, which runs here in the factory's process,
so Loki's credentials never reach them. Every line is scrubbed of emails, phone numbers
and tokens before it's written or returned.
"""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from software_factory import config
from software_factory.trace_cleaner import EMAIL_RE, PHONE_RE
from software_factory.utils import ssl_context

LOGS_DIR = "logs"
# Lines one query returns at most; Loki's own default is 100
LINE_LIMIT = 500
# Lines the query_logs tool shows the agent; the rest are in the file it writes
TOOL_LINES = 200
# A turn's log window reaches this far either side of its trace
TURN_MARGIN = timedelta(minutes=1)
JWT_RE = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*")
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}")
# The api's JSON log lines carry the request id the middleware binds
REQUEST_ID_RE = re.compile(r'"request_id"\s*:\s*"([^"]+)"')
ERRORS = '| json | level=~"(?i)warn(ing)?|error|critical"'


class LogsError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.LOKI_URL)


def scrub(line: str) -> str:
    line = JWT_RE.sub("<token>", line)
    line = BEARER_RE.sub(lambda m: f"{m.group(1)} <token>", line)
    return PHONE_RE.sub("<phone>", EMAIL_RE.sub("<email>", line))


def logql(contains: str | None = None, errors_only: bool = False, matching: str | None = None) -> str:
    """The query for the api's stream: lines containing a text, matching a regex, or errors."""
    query = config.LOKI_SELECTOR
    if contains:
        # JSON string escaping is what LogQL's double-quoted strings take
        query += f" |= {json.dumps(contains)}"
    if matching:
        query += f" |~ {json.dumps(matching)}"
    if errors_only:
        query += f" {ERRORS}"
    return query


def _client() -> httpx.AsyncClient:
    headers = {}
    if token := os.getenv("LOKI_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    if org := os.getenv("LOKI_ORG_ID"):
        headers["X-Scope-OrgID"] = org
    user = os.getenv("LOKI_USER")
    auth = (user, os.getenv("LOKI_PASSWORD") or "") if user else None
    return httpx.AsyncClient(
        base_url=config.LOKI_URL, headers=headers, auth=auth, verify=ssl_context(), timeout=30
    )


def _ns(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1_000_000_000))


async def query(start: datetime, end: datetime, logql_query: str, limit: int = LINE_LIMIT) -> list[tuple[str, str]]:
    """(time, line) pairs between start and end, oldest first, scrubbed."""
    if not enabled():
        raise LogsError("Loki isn't configured (SF_LOKI_URL)")
    params = {"query": logql_query, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": "forward"}
    try:
        async with _client() as client:
            response = await client.get("/loki/api/v1/query_range", params=params)
    except httpx.HTTPError as e:
        raise LogsError(f"Loki unreachable: {e}") from e
    if response.status_code != 200:
        raise LogsError(f"Loki answered {response.status_code}: {response.text[:300]}")
    entries = [
        (int(ns), line)
        for stream in response.json()["data"]["result"]
        for ns, line in stream["values"]
    ]
    return [
        (datetime.fromtimestamp(ns / 1e9, UTC).isoformat(timespec="milliseconds"), scrub(line))
        for ns, line in sorted(entries)
    ]


def write(ws: Path, name: str, entries: list[tuple[str, str]]) -> Path:
    path = ws / LOGS_DIR / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{at} {line}\n" for at, line in entries), encoding="utf-8")
    return path


def _merge(*groups: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted({entry for group in groups for entry in group})


async def turn_logs(ws: Path, conversation_id: str, turn: dict) -> tuple[Path, int]:
    """A chat turn's logs: the lines naming the conversation, every line of the requests
    they belong to, and the window's warnings and errors."""
    start = datetime.fromisoformat(turn["timestamp"].replace("Z", "+00:00")) - TURN_MARGIN
    end = start + TURN_MARGIN * 2 + timedelta(seconds=turn.get("latency") or 0)
    named = await query(start, end, logql(contains=conversation_id))
    request_ids = sorted({m.group(1) for _, line in named for m in REQUEST_ID_RE.finditer(line)})
    requests = (
        await query(start, end, logql(matching="|".join(re.escape(r) for r in request_ids)))
        if request_ids
        else []
    )
    errors = await query(start, end, logql(errors_only=True))
    entries = _merge(named, requests, errors)
    return write(ws, f"turn{turn['turn']}", entries), len(entries)


async def report_logs(ws: Path, reported_at: str) -> tuple[Path, int]:
    """Warnings and errors in the window before QA reported the bug."""
    end = datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
    entries = await query(end - timedelta(minutes=config.LOKI_WINDOW_MINUTES), end, logql(errors_only=True))
    return write(ws, "before-report", entries), len(entries)


def query_logs_tool(ws: Path):
    """The query_logs tool, bound to one issue's workspace."""
    from claude_agent_sdk import tool

    @tool(
        "query_logs",
        "Search the target's api logs in Loki (scrubbed of personal data). Times are ISO 8601 "
        "in UTC. contains filters to lines with that text (a conversation or request id, say); "
        "errors_only keeps warnings and errors. The lines are also saved to the issue's logs/.",
        {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "contains": {"type": "string"},
                "errors_only": {"type": "boolean"},
            },
            "required": ["since", "until"],
        },
    )
    async def query_logs(args):
        try:
            start = datetime.fromisoformat(args["since"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(args["until"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("give times with a zone, e.g. 2026-11-02T09:30:00Z")
            entries = await query(start, end, logql(args.get("contains"), bool(args.get("errors_only"))))
        except (ValueError, LogsError) as e:
            return {"content": [{"type": "text", "text": f"Couldn't query the logs: {e}"}], "is_error": True}
        name = "query-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = write(ws, name, entries)
        shown = "".join(f"{at} {line}\n" for at, line in entries[:TOOL_LINES])
        more = f"\n({len(entries) - TOOL_LINES} more lines in {path})" if len(entries) > TOOL_LINES else ""
        text = f"{len(entries)} lines, saved to {path}:\n{shown}{more}" if entries else "No lines in that window."
        return {"content": [{"type": "text", "text": text}]}

    return query_logs
