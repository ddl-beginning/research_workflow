# Troubleshooting

Start with the read-only diagnostic:

```powershell
workflow.exe doctor --probe-browser
```

Every failed check reports WHAT FAILED, WHY, and NEXT. Fix the first failed
dependency, then rerun the doctor.

## Installation and PATH

- `PATH_LAUNCHER_NOT_STABLE` or `workflow.exe` not found: run `.\install.ps1`
  from the Engine checkout, open a new PowerShell, and verify
  `Get-Command workflow.exe`.
- `INSTALLED_ENGINE_OUTDATED`: the command resolved to an older installed
  Engine than the checkout. Reinstall from the intended checkout and start a
  new shell.
- `INSTALLED_ENGINE_COMMIT_MISMATCH`: reinstall from the checkout whose code
  you intend to run.
- `SETUP_REQUIRED` or `RUNTIME_CONFIG_REQUIRED`: run `workflow.exe setup` from
  the Engine checkout.
- `MCP_NOT_REGISTERED` or `MCP_REGISTRATION_MISMATCH`: rerun setup to refresh
  the `research-supervisor` registration. Do not hand-edit credentials into
  the registration.

## Directory and plan errors

- `THIS_IS_WORKFLOW_ENGINE_REPOSITORY`: change to the Business Project
  directory. The Engine checkout is never a business workspace.
- `PLAN_INPUT_MISSING`: create both `plan/REQUIREMENTS.md` and
  `plan/STAGE_PLAN.md`.
- `MISSING_CANONICAL_PLAN_FILE`: use the exact expected filename shown by the
  diagnostic. In particular, `REQUIREMENT.md` is not accepted as a silent
  substitute for `REQUIREMENTS.md`.
- `AMBIGUOUS_PLAN_SOURCE`: choose one authoritative numbered requirements
  source and rename it to `REQUIREMENTS.md`. Workflow never guesses.
- `PROJECT_PLAN_INGESTION`: inspect the two source files and rerun
  `workflow.exe resume`; derived files are regenerated from the sources.

## Authentication and browser

- `CODEX_RUNTIME_UNAVAILABLE` or `AUTHENTICATION_REQUIRED`: verify the
  official Codex CLI installation and run `codex login`.
- `STANDARD_MODEL_UNAVAILABLE`: the configured standard Codex model is not
  available to this account; inspect the Codex installation/catalog and rerun
  doctor.
- `GPT_AUTH_REQUIRED`: sign in to ChatGPT in the machine-local persistent
  Browser Bridge profile, then rerun `workflow.exe doctor --probe-browser`.
- `BROWSER_HUMAN_VERIFICATION_REQUIRED`: complete the visible authentication
  or verification step. Human action is not a technical relay; do not paste
  cookies or tokens into the terminal.
- `BRIDGE_CONFIGURATION_REQUIRED`, `BRIDGE_SCRIPT_MISSING`,
  `BRIDGE_DEPENDENCIES_MISSING`, `BRIDGE_VERSION_MISMATCH`, or `NODE_NOT_FOUND`:
  run setup and verify Node/npm. The doctor reports the Bridge version and
  source digest so stale machine-local copies are visible.
- Browser closed/crashed: from the same Business Project run
  `workflow.exe resume`. Workflow recreates the process for that project's
  profile and keeps its plan, journal, and accepted history.
- Project URL missing: put the intended non-secret URL only in the `Workflow
  Binding` section of `plan/REQUIREMENTS.md`; do not put it in cookies or a
  machine config. A project URL must be the canonical ChatGPT Project route.
- `WORKFLOW_NOT_FOUND`: you are in a directory without a project identity or
  the two plan sources. Change to the Business Project and add the files.
- `HUMAN_APPROVAL_REQUIRED`: complete the explicit Human decision requested by
  the current project; Workflow will preserve the pending state.
- `CONSULTATION_EFFECT_UNRESOLVED`: inspect the bounded external receipt and
  rerun the documented recovery path; do not blindly repeat a side effect.

## Project state

Do not hand-create or hand-edit `.research/PROJECT_BRIEF.json`,
`.workflow-v2/journal.json`, receipts, or browser state. Workflow owns those
artifacts. Existing projects are resumed from their canonical state and
accepted work is not replayed merely because the plan views are regenerated.

For the ownership model, see [`architecture.md`](architecture.md). For the
browser boundary, see [`browser-lifecycle.md`](browser-lifecycle.md).
