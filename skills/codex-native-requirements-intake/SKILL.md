---
name: codex-native-requirements-intake
description: Start a bounded, project-level requirements intake before Stage work, with one user question at a time and an explicit brief approval gate.
metadata:
  short-description: Canonical project brief intake for Stage workflows
---

# Codex-native Requirements Intake

Use this skill when a user expresses a rough project requirement or asks to
make a project follow the bounded workflow. Intake is project-level and must
finish before any Stage contract is prepared.

## Operating contract

- Persist exactly one canonical brief at `.research/PROJECT_BRIEF.json`.
- Use `USER_CONFIRMED_BRIEF` when the user supplies a brief directly, or
  `CODEX_REQUIREMENTS_INTERVIEW` when Codex must elicit missing user goals.
- Ask no more than one core question in a turn. Ask only about user judgment:
  desired outcome and observable acceptance criteria. Facts available from the
  repository, files, or scripts are checked locally and are not questions.
- The only brief states are `DRAFT`, `WAITING_USER_APPROVAL`, `APPROVED`, and
  `CANCELLED`. `DRAFT` exposes `NEEDS_MORE_USER_INPUT` and one next question.
- User edits revise the same `project_id`; they do not create a second brief.

## Explicit gates and safety

Run the thin local CLI from the workflow repository:

```text
python scripts/requirements_cli.py --repo PROJECT requirements-init --rough-requirement "想做成这个工作流"
python scripts/requirements_cli.py --repo PROJECT requirements-show
python scripts/requirements_cli.py --repo PROJECT requirements-answer --answer "..."
python scripts/requirements_cli.py --repo PROJECT requirements-update --field success_criteria=...
python scripts/requirements_cli.py --repo PROJECT requirements-approve
python scripts/requirements_cli.py --repo PROJECT requirements-cancel --reason "..."
```

Approval is a record of the user's decision only. It must not start Project
Discovery, create/start a Stage, call ChatGPT/GPT, or mutate the target
project. Cancellation is terminal and fail-closed. Never save interview
transcripts, question files, raw GPT responses, credentials, browser state, or
duplicate briefs. The intake code-level guard requires `gpt_calls == 0` and
`external_calls == 0`.

## Step 12 hand-off

Only after the brief is explicitly `APPROVED` may the local Step 12 context
helper write `.research/PROJECT_CONTEXT.md`; all other brief states fail
closed. The hand-off is bounded and deterministic, retains no transcript, and
does not start discovery, a Stage, or a ChatGPT/bridge request. After this
approval boundary, Step 13 may be invoked explicitly through
`src/project_discovery.py` with an injected fake/real consultant and local
verifier; it performs one bounded FRESH consultation and writes only its
canonical discovery report. Step 14 may then explicitly build the bounded
`CODEX_FEASIBILITY`/project blueprint from the approved context and discovery
report, with one injected FRESH review and no Stage creation. A later explicit
Step 15 planning command may consume the verified Bootstrap outputs and
register one Stage as `PLANNED`; no later Stage action is implicit.
