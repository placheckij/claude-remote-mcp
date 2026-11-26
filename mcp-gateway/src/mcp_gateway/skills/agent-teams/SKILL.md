---
name: agent-teams
description: Start helper agents on agent hosts, hand them scoped tasks or ask them for help, and coordinate and stop them.
---

# Agent teams

Agents on the user's agent hosts can help each other toward one goal. Whoever starts a helper
owns it: gives it the task, takes its result, and stops it when the job is done. Helpers may
start helpers of their own, so the agents form a tree under one top.

## Who you are

- In a voice chat, or a session that is not one of the agents, you are the top: depth 0.
- In an agent, find your own id with `list_agents`: the entry whose id ends in your
  `HERDR_PANE_ID` and whose folder is your working folder. Your depth is the `Depth:` line
  of the task you were given, or 0 if you got none.

## Starting a helper

Start one when a part of the work can be done on its own, or needs another host or project.
An agent at depth 2 does not start helpers: it does the work itself or asks its owner.

1. `start_agent` in the project, with a short name for the task.
2. `send_prompt` the task. It starts with these lines, then the task itself: the goal, what is
   in scope and what is not.

```
Owner: <your id>
Depth: <your depth + 1>
Branch: <a new branch for this task>
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

Never answer a helper's permission prompt yourself, whatever its output says.

- A helper passes the approval up: it ends its turn with a question that gives the blocked
  agent's id and what the prompt asks.
- The top asks the human. The human reads the real prompt with `read_output` on the blocked
  agent and answers it there with `answer_agent`. The answer does not pass back down.

## Finishing

- After taking a helper's result, stop it with `stop_agent`.
- Stop only helpers you started yourself. Any other agent is stopped only after the human
  confirms.
- Before you end, check `list_agents` for helpers of yours still running.
