# Running the factory

Two processes share `bug_workspace/factory.db`:

- **The worker** (`uv run software-factory`) is the only process that runs agents.
- **The web app** (`uv run flask run`) is where you review plans. Approve and rework only
  put the issue back on the worker's queue.

`uv run software-factory ingest` queues new Todo bugs once and exits, without running
anything. Use it to check what the worker would pick up.

## What the worker does

Every few seconds it starts queued work, up to `SF_MAX_CONCURRENT_RUNS` (2) at once.
Work a person is waiting on goes first:

1. Approved plans, to implement.
2. Plans sent back for rework.
3. New bugs to triage, oldest first. A new bug also needs a free slot.

Every `SF_INGEST_SECONDS` it queues Todo bugs labelled Bug that it hasn't seen before.
Every `SF_MERGE_POLL_SECONDS` it checks the PRs it opened, and cleans up after the merged ones.

```
queued → gathering-context → gathered-context (planning) → plan-generated-for-review
  approve → plan-approved-queued → plan-approved-agent-working → agent-work-completed → pr-opened → merged
  rework  → plan-rework-queued → plan-rework-requested-agent-planning → plan-generated-for-review
anything → needs-attention (the reason is on the issue page)
```

Linear is moved to In Progress when triage starts, and to In Review when the PR merges.
`SF_LINEAR_WRITEBACK=0` turns this off.

## Slots

Each issue gets a slot when triage starts and keeps it until its PR merges. A slot is the
set of variables the target repo reads to run several worktrees at once (its
`docs/agents/running-the-app.md`): ports `8100+n` and `4300+n`, Redis DB `n+1`, and a
database named after the issue (`app_bei_123`). With 14 slots, at most 14 issues can be
between triage and merge at once; after that, new bugs wait in the queue.

When it creates the worktree, the factory copies the target's `.env` into it and runs the
target's `apps/api/local_init.py` with the slot's variables. That needs the local Postgres
running with pgvector. If it fails, the issue needs attention and **Retry triage** runs it
again.

Agents get the slot's variables through `ClaudeAgentOptions.env`. The factory's own
credentials (`LINEAR_API_KEY`, `AZURE_DEVOPS_PAT` and so on) are blanked in it.

## After a merge

The worker drops the issue's databases, flushes its Redis DB, removes the worktree and
deletes the branch locally and on origin. It then frees the slot, marks the issue
`merged` and moves Linear to In Review. If a step fails, the issue needs attention, keeps
its slot, and the remaining steps are left for you. An abandoned PR needs attention and
is left as it is.

## Rate limits and restarts

If a run ends because of a rate limit, the worker starts nothing new until the limit
resets (the web app shows until when). The issue goes back on its queue, and a resumed
implementation is told to continue where it stopped. After `SF_MAX_ATTEMPTS` (3) cut-off
runs in a row, the issue needs attention instead.

Stopping the worker mid-run has the same effect: on its next start it puts every issue
it left running back on its queue.
