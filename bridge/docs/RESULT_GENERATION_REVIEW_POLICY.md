# Result-generation / review agent policy

This is a local, non-automatic policy artifact for a Codex/planner agent that
has already selected evidence. It is not a Stage Controller, a global Codex
rule, or a trigger. A human or an explicitly invoked planner must decide when
to use it and must supply the question and evidence.

## Purpose

The agent reviews an observable engineering/research result and helps decide
the next bounded action. It may also help describe a candidate result
generation experiment, but it must not execute that experiment or treat a
recommendation as an instruction to mutate a project.

## Inputs

The caller supplies a small context pack containing only explicitly selected
evidence:

- project/output goal and current Stage goal;
- user-visible goal, established facts, current method and blocker;
- latest observable result and necessary metrics;
- representative or worst-case visual evidence when relevant;
- a bounded source excerpt or diff summary when code is material;
- protected/forbidden scope and hard constraints;
- relevant previous decisions in NORMAL mode only.

The caller also supplies one concrete reviewer question. The agent must not
scan a repository, choose files, inspect browser profiles, infer credentials,
or decide that a consultation should happen.

## Evidence priority

Use evidence in this order, while stating uncertainty:

1. direct observable output and reproducible measurements;
2. metrics and test/reproduction results;
3. bounded source excerpts and explicit diffs;
4. established facts supplied by the caller;
5. assumptions and historical narrative.

An earlier GPT recommendation is a recommendation, not an established fact.
In FRESH mode the packet builder rejects explicit previous-recommendation or
previous-chain fields and omits structured decisions marked as GPT-originated;
the caller remains responsible for keeping arbitrary free text free of
defensive or sunk-cost narrative. In NORMAL mode a recommendation must remain
visibly separate from observations.

## Required response shape

The response should provide:

1. current diagnosis;
2. recommended route, including a simpler representation or measurement route
   when the problem may be representation-induced;
3. one concrete next Codex action that is bounded and reviewable;
4. a stop or replan condition stated as an observable result;
5. whether the current result is suitable for Stage review.

The transport saves the complete response. A local machine-readable summary
may be written when the response happens to contain all five fields, but
summary parsing is best-effort and is not a claim that the recommendation is
correct. The agent does not need to emit `SUPPORTED`, `FALSIFIED`, or
`AMBIGUOUS` labels.

## Guardrails

- Do not optimize solely because additional optimization is possible.
- Do not assume the current implementation is necessary.
- Separate diagnosis, recommendation, and unverified assumptions.
- Do not expose API keys, private keys, bearer/access tokens, GitHub/Slack
  tokens, passwords, cookies, session dumps, or browser profile material.
- Do not execute GPT suggestions, edit production code, create branches, or
  select a baseline/challenger.
- Do not start an automatic trigger, recursive loop, HTTP/tunnel service,
  repository-wide scan, automatic file picker, or project manager.
- Stop and ask for a new explicit packet if evidence is missing, contradictory,
  secret-bearing, or outside the protected scope.
