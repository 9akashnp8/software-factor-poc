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
   A plan waiting on QA's answer to its question doesn't count.

Every `SF_INGEST_SECONDS` it queues Todo bugs labelled Bug that it hasn't seen before, and
finished ones (merged or released) that QA moved back to Todo. Every `SF_MERGE_POLL_SECONDS`
it checks the PRs it opened.

```
queued → gathering-context → gathered-context (planning) → plan-generated-for-review
  approve → plan-approved-queued → plan-approved-agent-working → verifying-fix → agent-work-completed → pr-opened
  rework  → plan-rework-queued → plan-rework-requested-agent-planning → plan-generated-for-review
  refresh → plan-refresh-queued → plan-refresh-agent-planning → plan-generated-for-review
pr-opened → merged-awaiting-deploy → deployed                         (merged, then a deploy succeeds)
pr-opened → pr-update-queued → pr-updating-agent-working → pr-opened   (conflicts, failed build)
needs-attention → pr-open-queued → agent-work-completed → pr-opened    (Open PR)
merged, deployed or released, back in Todo → queued                   (triaged again with history)
plan-generated-for-review → awaiting-qa-answer → plan-rework-queued        (QA question posted, then QA answered)
queued / plan-generated-for-review / awaiting-qa-answer / needs-attention → release-queued → releasing → released
anything → needs-attention (the reason is on the issue page)
```

Linear moves to In Progress when triage starts, to Pending Deployment when the PR merges,
and to In Review once a deploy ships the fix, with a comment for QA (below).
`SF_LINEAR_WRITEBACK=0` turns all of this off.

## Reproducing before planning

The planner reproduces the bug on its worktree, which is the latest `dev`, before it plans
a fix: a chat answer by replaying the conversation (below), anything else with a failing
test, the web app in a headless browser, or a call to the api. It records the outcome in
`reproduction.json` in the issue's workspace: `reproduced`, `not_reproduced` or
`not_attempted`, with the method, steps, evidence and, when it couldn't try, why. The plan
can't be presented until that file is valid.

The plan page shows it in a Reproduction section. A chat bug that wasn't reproduced by a
replay gets a "Not replayed" warning.

A bug that **didn't reproduce** gets findings instead of a fix plan, and a draft question
for QA. Approve is hidden. Edit the question and **Post to Linear** comments on the issue
(nothing is posted on its own) and adds the `Need Clarification` label
(`SF_LINEAR_CLARIFICATION_LABEL`; a team label of that name wins over a workspace one).
Posting moves the issue to *awaiting QA*, which doesn't count toward
`SF_MAX_PLANS_IN_REVIEW`, so new bugs keep being triaged while QA answers. When QA answers,
send the answer as rework: the planner tries again and updates `reproduction.json`.

### Replaying a chat turn

For a `cpcgpt-chat` bug, triage writes `replay.json` (each turn's question and answer,
redacted) next to `conversation.json`. Agents run

```
$SF_REPLAY --turn N --label before|after
```

It sends that turn's question with the history before it, trimmed as production trims it
(12 rows, then 1500 tokens), through the target's evaluation runner, 3 times. The answers
go to `replays/<label>-turn<N>.json`. It runs in the worktree's own environment, with the
target's `PROMPT_OVERRIDE_DIR` pointed at the worktree's `prompt-changes/`, so a prompt fix
is tried without touching Langfuse, and nothing is traced to Langfuse.

Limits: ERP answers come from the target's fixtures with a made-up employee, so bugs that
depend on one employee's real data won't reproduce; policy answers come from the sample
policies seeded when the slot is set up; turns that answer an approval or a form can't be
replayed.

### The browser

Planner and implementer get a headless Chrome through Playwright MCP
(`SF_PLAYWRIGHT_MCP`, a pinned `@playwright/mcp`, run with `npx`). It needs Node and Google
Chrome on the factory's machine; the first run downloads the package. Screenshots go to
`evidence/` in the workspace. Agents sign in with the target's `AWP_TEST_*` test accounts,
through the real identity provider. Those accounts have no ERP record, so the slots'
servers serve ERP answers from the target's fixtures (below). `SF_PLAYWRIGHT_MCP=` turns
the browser off.

## Verifying before the PR

The implementer turns the reproduction into a failing test where one fits and commits it
on its own (`test: reproduce BEI-12`), fixes the bug, then verifies: the test passes, the
relevant suites pass, a chat bug replays correctly (`--label after`), and, when the bug
shows in the web app, a browser check. **Require a browser check** on the approve button
makes the browser check mandatory.

The worker then checks what it reported, as `verifying-fix`:

- it re-runs the reproducing test itself, in the worktree with the slot's environment;
- a required browser check must have been done and passed;
- a failed check goes back to the implementer once, with the failure;
- still failing, or not verifiable at all (a service down, say), the issue needs
  attention with the reason, and **Open PR** ships it anyway if you decide to.

Servers the agent left running on the slot's ports are stopped afterwards, and again on
teardown.

The PR description starts with a before/after table (test, replay, browser), and the plan
page shows it with the screenshots.

## Logs

With `SF_LOKI_URL` set, triage fetches the app's logs (the api, the Celery worker and
beat) from Loki into `logs/` in the workspace: for a chat bug, the failing turn's requests (found through the conversation id)
and the warnings and errors around it; for any other bug, the warnings and errors in the
`SF_LOKI_WINDOW_MINUTES` before QA reported it. Agents search other windows with the
factory's `query_logs` tool. It runs in the worker, so Loki's credentials (`LOKI_TOKEN`, or
`LOKI_USER` and `LOKI_PASSWORD`) never reach an agent. Every line is scrubbed of emails,
phone numbers and tokens, and starts with the service that logged it (`[worker] …`). If
Loki is down, triage goes on and the issue's history says so.

`SF_LOKI_SELECTOR` picks the streams, in the labels the target's Alloy gives them (`env`,
`project`, `service`, `level`). The default reads the api, worker and beat of the
`agentic-app` project; add `env="…"` when other environments ship to the same Loki.

`SF_LOKI_URL` is Loki's own address, or Grafana's proxy for its Loki data source,
`https://<grafana>/api/datasources/proxy/uid/<uid>` (the uid is in the data source's
URL in Grafana). Through Grafana, `LOKI_TOKEN` is a Grafana service account token with
the Viewer role, and nothing else needs to reach Loki. To check it by hand:

```
uv run software-factory logs BEI-12 --since 2026-11-02T09:00:00Z --errors-only
```

## Metrics

`/metrics` shows, for the last 7, 30 or 90 days: how many plans were sent back, how many
fixes failed verification, how many bugs came back from QA, how reproduction went, why
issues needed attention, how long issues spent in each stage, and what the agent runs
cost. If many fixes come back from QA after passing verification, a check against the
deployed dev is worth adding.

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

When it creates the worktree, the factory copies the target's `.env` into it and sets the
slot up with the slot's variables, as the target's own commands would:

1. `apps/api/local_init.py` (`just init-local`) creates the slot's database. It needs the
   local Postgres running with pgvector.
2. `alembic upgrade head` (`just migrate`) creates its tables.
3. `apps/api/seed.py` (`just seed`) ingests the sample policies into it, for policy
   questions. It needs MinIO and the embedding model the `.env` names.
4. `npm ci` in `apps/web` installs the web app's packages, for the browser.

If 1 or 2 fail, the issue needs attention and **Retry triage** runs them again. If 3 or 4
fail, the issue's history says so and triage goes on: only policy replays and the browser
need them. The worktree's `.env` comes from the target's main checkout, so the slot's
servers use whatever Langfuse and model settings that file has.

ERP is the exception: the slot's variables set `CPC_ERP_MOCK_ENABLED=true`, so its servers
answer ERP calls from the target's captured fixtures, with the same made-up employee for
any user, as replays do. The test accounts get a complete profile, and agents can't
submit anything to the real ERP. Bugs that depend on one employee's real ERP data won't
reproduce. `SF_SLOT_ERP_MOCK=0` uses the ERP the `.env` names instead.

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
>
> **Reproduced:** With an automated test, which failed before the fix.
>
> **Checked:** …

The implementing agent writes the root cause, the fix and how it checked it for QA when it
finishes, in plain sentences without code, file names or user data; they're kept in the
workspace's `qa.json`. The Reproduced line comes from the planner's `reproduction.json`. Emails and phone numbers are scrubbed again before posting. Issues implemented
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
