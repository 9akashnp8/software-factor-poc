# Running the factory

Two processes share `bug_workspace/factory.db`:

- **The worker** (`uv run software-factory`) is the only process that runs agents.
- **The web app** (`uv run flask run`) is where you review plans. Approve, rework and
  refresh only put the issue back on the worker's queue.

`uv run software-factory ingest` queues new Todo bugs once and exits, without running
anything. Use it to check what the worker would pick up.

## What the worker does

Every few seconds it starts queued work, up to `SF_MAX_CONCURRENT_RUNS` (2) at once.
Work a person is waiting on goes first:

1. Approved plans, to implement.
2. Plans sent back for rework, then plans you asked to refresh.
3. Opened PRs that need an update (see below), and issues you released.
4. New bugs to triage, oldest first. A new bug also needs a free slot, and waits while
   `SF_MAX_PLANS_IN_REVIEW` (3) plans are being written, reworked or waiting for your review.

Every `SF_INGEST_SECONDS` it queues Todo bugs labelled Bug that it hasn't seen before, and
finished ones (merged or released) that QA moved back to Todo. Every `SF_MERGE_POLL_SECONDS`
it checks the PRs it opened.

```
queued → gathering-context → gathered-context (planning) → plan-generated-for-review
  approve → plan-approved-queued → plan-approved-agent-working → agent-work-completed → pr-opened
  rework  → plan-rework-queued → plan-rework-requested-agent-planning → plan-generated-for-review
  refresh → plan-refresh-queued → plan-refresh-agent-planning → plan-generated-for-review
pr-opened → merged-awaiting-deploy → deployed                         (merged, then a deploy succeeds)
pr-opened → pr-update-queued → pr-updating-agent-working → pr-opened   (conflicts, failed build)
needs-attention → pr-open-queued → agent-work-completed → pr-opened    (Open PR)
merged, deployed or released, back in Todo → queued                   (triaged again with history)
queued / plan-generated-for-review / needs-attention → release-queued → releasing → released
anything → needs-attention (the reason is on the issue page)
```

Linear moves to In Progress when triage starts, to Pending Deployment when the PR merges,
and to In Review once a deploy ships the fix, with a comment for QA (below).
`SF_LINEAR_WRITEBACK=0` turns all of this off.

## Refreshing a plan

A plan is written on the `dev` its worktree was created from. While it waits for review,
other PRs merge. The plan page says how many, naming the factory fixes among them: "3 new
merges to dev, including BEI-20 and BEI-25 since this plan was written". The count uses
`origin/dev` as the worker last fetched it, every `SF_MERGE_POLL_SECONDS`.

**Refresh plan** fast-forwards the worktree to the latest `dev` and resumes the planning
session. The prompt lists each merge with its files, and the root cause and fix for factory
fixes. The planner either revises the plan, which starts with a "Revised after dev moved"
note, or replies why it still holds. Either way the issue comes back to
`plan-generated-for-review`, and its history says which. Approve and Rework are hidden while
it runs.

It runs no agent when `dev` hasn't moved since the plan. It refuses, and leaves the worktree
alone, when the branch already has commits or the worktree has uncommitted files. If the
run doesn't finish, the plan is unchanged and the page still counts the merges, so you can
refresh again. Nothing refreshes on its own, and nothing fast-forwards once a plan is
approved: a later conflict or failed build goes back to the agent as a PR update.

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

The worker records the merge commit, marks the issue `merged-awaiting-deploy` and moves
Linear to Pending Deployment. It then drops the issue's databases, flushes its Redis DB,
removes the worktree, deletes the branch locally and on origin, and frees the slot. If a
cleanup step fails, the issue keeps its slot and its status, the failure is on its
history, and the step is retried on every PR check until it succeeds. An abandoned PR
needs attention and is left as it is, until you release it.

## Deployment and the QA comment

The deploy pipeline (`SF_DEPLOY_PIPELINE_ID`, 97: `dev-agentic-workplace-platform`) runs
once for every merge into dev. A fix counts as deployed when a run of it queued after the
merge succeeds: its own run, or a later one that also contains it. Each failed run is
recorded once on the issue's history with the line that explains it (a full agent disk,
an image pull timing out), and the issue keeps waiting for the next deploy.

Once deployed, the issue is marked `deployed`, and Linear gets a comment and moves to In
Review:

> **Fix deployed to dev** · [PR 5541](…) · [build 8890](…)
>
> **Root cause:** …
>
> **Fix:** …

The implementing agent writes the root cause and fix for QA when it finishes, in plain
sentences without code, file names or user data; they're kept in the workspace's
`qa.json`. Emails and phone numbers are scrubbed again before posting. Issues implemented
before this existed get the first line only.

With `SF_DEPLOY_PIPELINE_ID=0`, there's no Pending Deployment: the issue is marked
`merged`, and the comment ("Fix merged into dev") and In Review come at merge.

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

## Open PR

When the implementation succeeded but pushing or opening the PR didn't (a push rejected,
Azure DevOps unreachable, files left uncommitted), the issue needs attention and offers
**Open PR**. That pushes the committed fix and opens its PR without running the agent again.

The worktree check counts a file as uncommitted only if its content differs from the
last commit, or it's untracked. With `core.autocrlf` on, the target's commit hook
(prettier) rewrites files with LF endings, and `git status` then lists them as modified
though nothing changed; those don't count.

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
