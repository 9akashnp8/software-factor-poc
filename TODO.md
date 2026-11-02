# TODO

What's still open after the review work on `claude/software-factory-review-99836a`. How
the factory works today is in `docs/running.md` and `docs/agent-permissions.md`.

## Outside the code

- [ ] **Dev deploys fail on the build agent.** Pulls from ghcr.io time out (builds 8872,
      8873) and the agent's disk is full (8874 onwards). Until both are fixed, merged issues
      wait at Pending Deployment (BEI-13, BEI-15 today).
- [ ] **`dev-awp-pr-check` hasn't run on PRs since 20 October** (last: PR 5476). Without
      it, PRs that merge cleanly but break the build go unnoticed.
- [ ] **Move BEI-12 to In Review in Linear by hand** (PR 5534). It merged before the deploy
      flow existed, and its In Review move failed because the state was missing then.
- [ ] **Remove the hard-coded ERP bearer token** from the target's main checkout:
      `packages/cpc_erp_client/src/cpc_erp_client/client.py` has an uncommitted change
      that sends a fixed JWT, which looks like a real person's, instead of `token`. Worktrees
      don't have it, but it's a secret in the working tree and anything run from that
      checkout calls ERP as that person.

## Planned next: reproduce and replay

Plans are approved without evidence that the bug happens, and PRs don't show it's gone.

- [ ] **Replay input:** at triage, write `replay.json` (the turns and the failing turn,
      redacted) from `conversation.json`.
- [ ] **Replay tool in the factory** (`replay_turn.py`), run with the worktree's environment
      via `$SF_REPLAY --turn N --label before|after`:
      - calls the target's `api.evals.runner.run_question` in-process (no auth needed; ERP
        answers from fixtures);
      - trims history the way production does;
      - runs 3 times;
      - records the results to the issue's workspace.
- [ ] **Local prompt override:** the replay tool serves `prompt-changes/<name>.md` in place
      of the Langfuse prompt, so prompt fixes are tested without saving anything in
      Langfuse, where every new version goes live at once.
- [ ] **Agent instructions:** reproduce on the latest dev before planning, replay after
      implementing, and write prompt changes as full system messages in Langfuse template
      syntax. The planner reports one of: reproduced (steps and failing output), not
      reproduced (check recent merges for a fix before planning anything), or couldn't
      attempt (and why).
- [ ] **Failing test first** for bugs that aren't chat answers: the implementer writes the
      test, records it failing, then fixes. Today it's told to "add or update a test where
      one fits", and a test written after the fix may never have failed.
- [ ] **Not reproduced → a question for QA, posted by a person.** The plan page shows the
      planner's draft comment asking QA for detail, with a button to post it to Linear.
      Nothing is posted automatically in v1.
- [ ] **Show it:** a Reproduction section on the plan page (warn if a chat bug has none),
      and a before/after table in the PR description and in the Linear QA comment
      (`deployments.comment_body`).
- Known limits: bugs that depend on one employee's real ERP data won't reproduce locally;
  policy answers need the slot's database seeded.

## Not built yet

- [ ] **API logs as context**, from the shared Loki once the target's VM ships its
      docker compose logs there. A factory tool (`$SF_LOGS --since --until --grep`, like
      `$SF_REPLAY`) runs a read-only LogQL query, scrubs it like `trace_cleaner`, and saves
      it to the issue's workspace. Triage pre-fetches a window: the trace's turn times for
      chat bugs, the issue's creation time for others. Filter by user and errors, not time
      alone. Agents get the tool, never Loki's credentials.
- [ ] **Factory metrics:** plans sent back for rework, verification failures, bugs that
      come back after a fix, and how long each stage takes. They show whether the factory
      is getting better, and a rise in bugs that come back would mean a check after deploy
      is needed after all.
- [ ] **Changes in Linear after an issue is queued.** Re-read the issue before each job:
      cancelled or duplicate means stop and release the slot; an edited description means
      plan again.
- [ ] **Duplicate bugs are planned in parallel.** Give the planner the titles and plan
      summaries of issues already in flight, so it can say "same root cause as BEI-12".
- [ ] **Reviewer comments on the PR.** "Changes requested" in Azure DevOps reaches nobody;
      queue an update job with the comments, as conflicts and failed builds do.
- [ ] **Stop servers agents leave running.** Teardown doesn't stop an app an agent
      started. The slot's ports stay taken, and on Windows the worktree can't be removed
      while its files are in use, so the cleanup retry keeps failing.
- [ ] **Track prompt fixes to apply in Langfuse.** A merged PR with `prompt-changes/*.md`
      should show up as "prompt to apply" until someone has.
- [ ] **Overlap warning noise.** Nearly every plan mentions
      `docs/agents/running-the-app.md`, so the warning flags every other issue. Ignore files
      that plans only read.

## Deferred on purpose

- [ ] **Sandbox and network limits for agents**, when this moves to a Linux VM (native
      Windows has no sandbox). Options are in `docs/agent-permissions.md`.
- [ ] **Planning can change files through shell commands** while `useAutoModeDuringPlan`
      is on; kept so planners can run tests. Revisit with the sandbox.
- [ ] **Review gate** (`review_and_gate` in `main.py`): restored but disabled in
      `implement_and_review` until its behaviour has been checked.

## Built, not yet run for real

- [ ] **Root cause and fix in the Linear QA comment.** The implementer's structured output
      hasn't run in a real implementation yet.
- [ ] **The full deploy flow:** merge → Pending Deployment → In Review with the comment.
      It can't complete until a dev deploy succeeds.
- [ ] **Refresh plan.** Not yet run on a real plan. BEI-26 and BEI-27 are in review with
      BEI-20 and BEI-25 merged since; a refresh of either is the first real test.

## Before others use the web app

- [ ] **HTTPS in front of the web app:** Basic auth sends passwords readably over plain HTTP.
- [ ] **Langfuse over HTTPS:** its token travels over plain HTTP on the internal network.
