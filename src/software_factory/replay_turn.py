"""Replay one turn of a recorded chat against a worktree's code: what agents run as $SF_REPLAY.

    $SF_REPLAY --turn 3 --label before

It runs in the target worktree's own environment, not the factory's: the factory sets
SF_REPLAY to `uv run --all-packages --project <worktree> python <this file> --worktree
<worktree>`. So it imports nothing from the factory, and the target's modules only inside
main().

It sends the turn's question with the history before it, trimmed the way production
trims it, through the target's evaluation runner (api.evals.runner.run_question), RUNS
times. ERP answers come from the target's fixtures, as in its evaluations. Prompts in
<worktree>/prompt-changes replace Langfuse's (the target's PROMPT_OVERRIDE_DIR), and nothing
is traced to Langfuse. The answers go to replays/<label>-turn<N>.json in the issue's
workspace (SF_WORKSPACE); judging them is the agent's job.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

RUNS = 3
ANSWER_PREVIEW_CHARS = 300


class ReplayError(RuntimeError):
    pass


def find_turn(replay: dict, number: int) -> dict:
    turn = next((t for t in replay["turns"] if t["turn"] == number), None)
    if turn is None:
        raise ReplayError(f"No turn {number}; the conversation has turns 1-{len(replay['turns'])}")
    if not turn["user_query"]:
        raise ReplayError(
            f"Turn {number} answers an approval or a form, not a question; replaying it isn't supported"
        )
    return turn


def history_rows(replay: dict, number: int) -> list[dict]:
    """The messages before turn number as production stores them: each question, then its answer.

    Turns that answered an approval or a form are left out: production rewrites those rows
    ("Approved.") from data the trace doesn't keep.
    """
    rows = []
    for turn in replay["turns"]:
        if turn["turn"] >= number:
            break
        if turn["user_query"]:
            rows.append({"role": "user", "content": turn["user_query"]})
        if turn["answer"]:
            rows.append({"role": "assistant", "content": turn["answer"]})
    return rows


def production_history(rows: list[dict], row_limit: int, trim: Callable[[list[dict]], list[dict]]) -> list[dict]:
    """What production would send as history: the last row_limit rows, then trimmed by tokens."""
    return trim(rows[-row_limit:]) if row_limit else trim(rows)


def output_path(workspace: Path, label: str, number: int) -> Path:
    return workspace / "replays" / f"{label}-turn{number}.json"


def summary(record: dict) -> str:
    lines = [f"Turn {record['turn']} ({record['label']}), {len(record['runs'])} runs, saved to {record['path']}"]
    for n, run in enumerate(record["runs"], start=1):
        answer = (run.get("answer") or run.get("error") or "").strip().replace("\n", " ")
        if len(answer) > ANSWER_PREVIEW_CHARS:
            answer = answer[:ANSWER_PREVIEW_CHARS] + "…"
        tools = ", ".join(run.get("tool_calls") or []) or "no tools"
        lines.append(f"{n}. {run['outcome']} via {run.get('agent') or '?'} ({tools}): {answer}")
    return "\n".join(lines)


def _langchain_trim(token_limit: int) -> Callable[[list[dict]], list[dict]]:
    """build_history's trim_messages call, on {role, content} dicts."""
    from langchain_core.messages import AIMessage, HumanMessage, trim_messages
    from langchain_core.messages.utils import count_tokens_approximately

    def trim(rows: list[dict]) -> list[dict]:
        messages = [HumanMessage(r["content"]) if r["role"] == "user" else AIMessage(r["content"]) for r in rows]
        kept = trim_messages(
            messages,
            max_tokens=token_limit,
            token_counter=count_tokens_approximately,
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        return [{"role": "user" if m.type == "human" else "assistant", "content": m.content} for m in kept]

    return trim


async def _run(question: str, history: list[dict], runs: int) -> list[dict]:
    from agents.router import workflow
    from api.db.session import AsyncSessionLocal
    from api.evals.contexts import reranker_from_settings, resolve_contexts
    from api.evals.runner import run_question
    from api.config import settings
    from knowledge.vectors import build_pgvector_factory

    vector_stores = build_pgvector_factory(dsn=settings.pgvector_dsn)
    try:
        knowledge_context, policy_context = await resolve_contexts(
            AsyncSessionLocal, vector_stores, reranker_from_settings()
        )
        results = []
        for _ in range(runs):
            started = time.monotonic()
            result = await run_question(
                question,
                graph=workflow,
                knowledge_context=knowledge_context,
                policy_context=policy_context,
                history=history,
            )
            results.append({**result.as_output(), "seconds": round(time.monotonic() - started, 1)})
        return results
    finally:
        vector_stores.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one turn of the issue's recorded chat.")
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--turn", required=True, type=int)
    parser.add_argument("--label", required=True, help="before (reproducing) or after (verifying the fix)")
    parser.add_argument("--runs", type=int, default=RUNS)
    args = parser.parse_args(argv)

    workspace = Path(os.environ.get("SF_WORKSPACE", ""))
    if not (workspace / "replay.json").is_file():
        print(f"No replay.json in SF_WORKSPACE ({workspace}); only chat bugs can be replayed.", file=sys.stderr)
        return 2
    worktree = args.worktree.resolve()
    replay = json.loads((workspace / "replay.json").read_text(encoding="utf-8"))
    try:
        turn = find_turn(replay, args.turn)
    except ReplayError as e:
        print(e, file=sys.stderr)
        return 2

    # Before the target's modules load: they read these at import
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
    overrides = worktree / "prompt-changes"
    if overrides.is_dir():
        os.environ["PROMPT_OVERRIDE_DIR"] = str(overrides)
    # The target's api package lives in apps/, as `uv run python -m api.evals` sees it from there
    sys.path.insert(0, str(worktree / "apps"))

    from api.services.conversation import HISTORY_ROW_LIMIT, HISTORY_TOKEN_LIMIT

    history = production_history(history_rows(replay, args.turn), HISTORY_ROW_LIMIT, _langchain_trim(HISTORY_TOKEN_LIMIT))
    runs = asyncio.run(_run(turn["user_query"], history, args.runs))

    path = output_path(workspace, args.label, args.turn)
    path.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    record = {
        "label": args.label,
        "turn": args.turn,
        "question": turn["user_query"],
        "recorded_answer": turn["answer"],
        "history_messages": len(history),
        "commit": commit.stdout.strip() or None,
        "prompt_overrides": sorted(str(p.relative_to(overrides)) for p in overrides.rglob("*.md")) if overrides.is_dir() else [],
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": runs,
        "path": str(path),
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(summary(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
