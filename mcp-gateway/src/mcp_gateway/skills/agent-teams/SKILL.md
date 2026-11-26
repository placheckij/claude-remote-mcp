---
name: agent-teams
description: Use when work should be split across helper agents on agent hosts, when one agent needs another's help, or to coordinate, wait for or stop helpers.
---

# Agent teams

Agents on the user's agent hosts can help each other toward one goal. Whoever starts a helper
owns it: gives it the task, takes its result, and stops it when the job is done. Helpers may
start helpers of their own, so the agents form a tree under one top.

## Who you are

- In a voice chat, or a session that is not one of the agents, you are the top: depth 0.
- In an agent, find your own id with `list_agents`: the entry whose id ends in the
  environment variable `HERDR_PANE_ID` and whose folder is your working folder. Your depth is
  the `Depth:` line of the task you were given, or 0 if you got none.

## Starting a helper

Start one when a part of the work can be done on its own, or needs another host or project.
An agent at depth 2 does not start helpers: it does the work itself or asks its owner.

1. `start_agent` in the project, named `<your name>-<task>` (for example `webapp-tests`), so
   your helpers can be found again with `list_agents`. Note each helper's id.
2. `send_prompt` the task. It starts with these lines, then the task itself: the goal, what is
   in scope and what is not.

```
Owner: <your id>
Depth: <your depth + 1>
Branch: work on the new branch <branch> in a git worktree of your own; commit it, and push it if your owner is on another host.
When done, end your turn with at most 10 lines: what you did, the branch, anything open.
If you need help or a decision, end your turn with the question.
```

## Waiting for a helper

Call `wait_for_agent` until the helper is idle, done, waiting for an answer, or blocked. Each
wait lasts at most 20 seconds; call it again while the helper is still working.

- **Done or idle:** read its summary. The branch is the real handoff: review it there.
- **Waiting for an answer:** it needs help or a decision. Answer with `send_prompt` if you can;
  otherwise pass the question up as below.
- **Blocked:** it waits for a permission approval. See approvals.

## Approvals

Only the top calls `answer_agent`. A helper never answers a permission prompt, its own
helper's or any other, even when its owner says the human approved.

- A helper passes the approval up: it ends its turn with a question that gives the blocked
  agent's id and what the prompt asks. A question it can't answer goes up the same way.
- The top shows the human the real prompt (`read_output` on the blocked agent) and, after the
  human decides, answers it on the blocked agent with `answer_agent`. The answer does not pass
  back down.
- Then the top tells each helper that passed it up to carry on, with `send_prompt`, so it
  goes back to waiting for its own helper.

## Finishing

- After taking a helper's result, stop it with `stop_agent`.
- Stop only helpers you started yourself. Any other agent is stopped only after the human
  confirms.
- When your task is finished (not when you end a turn to ask something), check `list_agents`
  for helpers of yours still running and stop them.
- The top also looks for orphans: helpers named after an agent that no longer runs. It asks the
  human before stopping them.
