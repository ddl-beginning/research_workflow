# Architecture and ownership

Research Workflow has three boundaries:

```text
Engine repository  -> reusable code, schemas, Bridge source, tests, docs
Business Project   -> source/data, two plan files, project identity and history
Machine             -> launcher, Python/Node/Codex configuration, browser profile
```

The Engine checkout is not a business project. A business Stage must be
started from the Business Project directory. The installed launcher may point
to the Engine checkout, but all project identity and Workflow state are
resolved relative to the current Business Project.

## Public repository

- `src/` contains the installed Python runtime.
- `bridge/` contains the versioned Browser Bridge source and lockfile.
- `schemas/` contains contracts.
- `scripts/` contains the CLI, installer integration, doctor, and MCP entry.
- `skills/`, `templates/`, `examples/`, and `fixtures/` are reusable assets.
- `tests/` contains offline and seam tests.
- `docs/` contains user and developer documentation; `docs/history/` contains
  historical release records.

## Business Project

The user-owned planning boundary is:

```text
<PROJECT_ROOT>/plan/REQUIREMENTS.md
<PROJECT_ROOT>/plan/STAGE_PLAN.md
```

Workflow normalizes those sources into the existing canonical project brief,
the V2 journal, and the derived `plan/WORKFLOW_PLAN.md` and
`plan/CURRENT_STATE.md`. The two source files remain human-owned and are never
overwritten by the derived views.

The canonical lifecycle authority remains the existing StageController,
journal, contracts, and schemas. The plan-ingestion layer is only an input
normalizer and projection; Markdown cannot change lifecycle state by itself.

## Machine-local boundary

`workflow.exe setup` stores non-secret runtime configuration and the provisioned
Bridge under `%LOCALAPPDATA%\ResearchWorkflow`. The stable launcher is under
`%LOCALAPPDATA%\ResearchWorkflow\bin`. Codex login and the persistent browser
profile remain machine-local. No cookie, token, password, or browser storage is
copied into the repository or between machines.

## Project browser binding

An optional ChatGPT Project URL is read only from the `Workflow Binding`
section of `REQUIREMENTS.md` and is recorded in the canonical project brief.
The Browser Bridge keeps the browser/profile association project-scoped and
rejects cross-project continuation. See
[`browser-lifecycle.md`](browser-lifecycle.md).
