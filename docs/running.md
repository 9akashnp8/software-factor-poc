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
3. Opened PRs that need an update (see below), and issues you released.
4. New bugs to triage, oldest first. A new bug also needs a free slot, and waits while
   `SF_MAX_PLANS_IN_REVIEW` (3) plans are being written, reworked or waiting for your review.

Every `SF_INGEST_SECONDS` it queues Todo bugs labelled Bug that it hasn't seen before, and
finished ones (merged or released) that QA moved back to Todo. Every `SF_MERGE_POLL_SECONDS`
it checks the PRs it opened.

```
queued → gathering-context → gathered-context (planning) → plan-generated-for-review
  approve → plan-approved-queued → plan-approved-agent-working → agent-work-completed → pr-opened → merged
  rework  → plan-rework-queued → plan-rework-requested-agent-planning → plan-generated-for-review
pr-opened → pr-update-queued → pr-updating-agent-working → pr-opened   (conflicts, failed build)
merged or released, back in Todo → queued                             (triaged again with history)
queued / plan-generated-for-review / needs-attention → release-queued → releasing → released
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
is left as it is, until you release it.

## PRs that conflict or fail their build

Parallel fixes can touch the same code, so whichever PR merges second may conflict, or merge
cleanly and still break the build. Two fixes that each add an Alembic migration are the
usual case: git sees no conflict, but `alembic upgrade` then finds two heads.

The worker reads each open PR from Azure DevOps. If its merge status is `conflicts`, or the
newest build of its merge ref (`refs/pull/<id>/merge`, the PR combined with the latest
base) failed, the issue goes back to its agent with the reason, and with the build's
errors for a failed build. The agent resumes its implementation session, merges
`origin/dev` in (a merge, not a rebase, so the push needs no force and review comments
stay attached), resolves the conflicts, adds an Alembic merge migration if there are two
heads, fixes the build, runs the tests and commits. The worker then pushes to the same PR.

`AZURE_DEVOPS_PAT` needs **Code (Read & Write)** to open and read PRs, and **Build (Read)**
to see their builds. Without Build (Read), merges and conflicts are still handled, failed
builds go unnoticed, and the worker log says so on every check.

Each conflict (a pair of source and target commits) and each failed build is acted on
once. After `SF_MAX_UPDATE_ROUNDS` (3) rounds for one PR, or when the agent leaves the
merge unresolved, the issue needs attention.

When you review a plan, the page warns if the files it names are also in another issue's
plan or branch. It's a warning only: approving both is fine.

## Releasing an issue

**Release** (on queued, planned and needs-attention issues) gives up on an issue. The
worker tears down what it holds, as after a merge but keeping the remote branch, and marks
it `released`. Linear and any
PR are left as they are. Use it for an abandoned PR, or a bug you decide not to fix. If QA
later moves the issue back to Todo, it's triaged again.

## Resetting by hand

When something breaks mid-way and the buttons don't get an issue moving again:

```
uv run software-factory reset BEI-12              # start over: triage from scratch
uv run software-factory reset BEI-12 --to plan    # back to plan review, keeping the worktree
uv run software-factory reset --all               # start every unfinished issue over
```

Starting over tears down what the issue holds (databases, Redis DB, worktree, local branch,
slot), moves its last plan, report and issue context into `reset-<time>/` in its workspace,
and queues it for triage with a new session. `--to plan` only changes the status, so you
can approve or rework the same plan again; it needs a plan to go back to.

`--all` asks before it starts, unless you pass `--yes`. It skips merged and released
issues, and lifts any rate-limit pause.

A reset refuses while an issue has a run in progress: stop the worker first. If the
worker crashed and left a run open, `--force` closes it. A reset never touches Linear,
Azure DevOps or a pushed branch. If the issue had an open PR, abandon it and delete its
remote branch, or the next attempt's push is rejected; the reset says so when it applies.

## Bugs that come back

When a merged or released issue shows up in Todo again, the fix didn't hold. The worker
moves its plan, report and review into `previous-attempt/` in the issue's workspace and
queues it for triage. The new planning run sees the earlier plan, report and PR link, and
is asked to find out why that fix didn't work before planning again.

## Rate limits and restarts

If a run ends because of a rate limit, the worker starts nothing new until the limit
resets (the web app shows until when). The issue goes back on its queue, and a resumed
implementation is told to continue where it stopped. After `SF_MAX_ATTEMPTS` (3) cut-off
runs in a row, the issue needs attention instead.

Stopping the worker mid-run has the same effect: on its next start it puts every issue
it left running back on its queue.
