# Research Workflow

Research Workflow is a local engine for bounded research and engineering
projects. It keeps the project goal and acceptance criteria visible to the
Human, lets GPT research, plan, and review technical choices, lets Codex
implement and test, and coordinates the bounded Workflow lifecycle and
recovery around the project.

The GitHub repository is the Workflow Engine. Your source, data, plans, and
project history belong in a separate Business Project directory.

## 5-minute Windows installation

Prerequisites: Windows PowerShell, Python 3.11 or newer, Node.js/npm, and the
official Codex CLI. From PowerShell:

```powershell
git clone https://github.com/ddl-beginning/research_workflow.git
Set-Location research_workflow
git checkout workflow-v2.1.7-stable
.\install.ps1
```

`install.ps1` installs this checkout and creates the stable launcher at
`%LOCALAPPDATA%\ResearchWorkflow\bin\workflow.exe`. It also adds that
directory to the Windows user PATH. Open a new PowerShell so the persisted PATH
is loaded, then verify the installation:

```powershell
Get-Command workflow.exe
workflow.exe --version
workflow.exe setup
workflow.exe doctor --probe-browser
```

The fresh-shell check is passed only when `Get-Command workflow.exe` resolves
the stable launcher. You do not need to find a Python `Scripts` directory.
`workflow.exe setup` provisions the packaged Browser Bridge and its npm
dependencies into machine-local storage; it does not install a second Engine
checkout. If setup reports a login requirement, complete the normal login and
run the command again.

## Engine vs Business Project

Keep the two directories separate:

```text
D:\work\
├─ research_workflow\
│  └─ Workflow Engine
│
└─ my_business_project\
   ├─ source / data / existing project files...
   └─ plan\
      ├─ REQUIREMENTS.md
      └─ STAGE_PLAN.md
```

`research_workflow` is where the Engine is installed or developed. Do not run
your business Stages from the Engine checkout. Start Workflow after changing
directory to `my_business_project`.

## Start a project with REQUIREMENTS.md + STAGE_PLAN.md

The formal user inputs are exactly:

```text
plan/REQUIREMENTS.md
plan/STAGE_PLAN.md
```

They are ordinary Markdown. `REQUIREMENTS.md` describes the final goal,
scope, outputs, constraints, and acceptance. `STAGE_PLAN.md` describes the
independently verifiable Stages, inputs, outputs, and acceptance for each
Stage.

For a new project containing those two files, the canonical startup command is:

```powershell
Set-Location D:\work\my_business_project
workflow.exe resume
```

Workflow reads both sources, creates the canonical project identity, ingests
the plan, and derives:

```text
plan/WORKFLOW_PLAN.md
plan/CURRENT_STATE.md
```

The canonical brief, journal, receipts, and browser binding are created by
Workflow. Do not hand-edit `PROJECT_BRIEF.json`, create a journal, or create a
receipt. The no-argument command is also supported: when the two plan files
are present it selects the same `resume` route.

Use these exact names. These are wrong and are never silently renamed:

```text
REQUIREMENT.md
REQUIREMENTS (1).md
REQUIREMENTS (2).md
stageplan.md
```

If `REQUIREMENTS.md` is missing but `REQUIREMENT.md` is found, Workflow prints
`EXPECTED: plan/REQUIREMENTS.md`, `FOUND POSSIBLE MATCH:
plan/REQUIREMENT.md`, and asks you to rename the human source. If multiple
numbered requirements files are found, it prints `AMBIGUOUS_PLAN_SOURCE` and
lists the candidates. Choose one authoritative source and name it
`REQUIREMENTS.md`; Workflow does not guess.

## Existing Project

For an existing project such as
`D:\work\pointcloud\structural_surface_frontend`, add or verify the same two
files under `plan/`, then run the same command:

```powershell
Set-Location D:\work\pointcloud\structural_surface_frontend
workflow.exe resume
```

Workflow detects existing identity, history, and accepted work, aligns the
plan, and resumes the next legal Stage. It does not destructively bootstrap
the source tree or replay an accepted Stage.

## First-time authentication

For Codex, use the official login flow:

```powershell
codex login
```

The Browser Bridge uses one machine-local persistent browser profile per
business project. The first time a Browser window appears, sign in to ChatGPT
normally. Never copy cookies, tokens, passwords, or the browser profile to
another computer, and never commit `.auth` or browser state to Git. Each
computer authenticates independently.

Human verification is only for authentication. Workflow handles technical
browser recovery; it does not require a Human to relay technical actions.

## Everyday commands

Run these from the Business Project directory unless stated otherwise:

```powershell
workflow.exe resume
workflow.exe status
workflow.exe doctor
workflow.exe doctor --probe-browser
workflow.exe --version
```

If you are maintaining the Engine, run `git pull`, check out the desired
stable tag, and rerun `.\install.ps1` from the Engine checkout. Then open a new
PowerShell and verify `workflow.exe --version` before resuming a project.

## Browser / authentication recovery

One business project has one persistent Project Browser/profile. If the
browser is closed or crashes, run:

```powershell
workflow.exe resume
```

Workflow rebuilds the process for the same project-specific profile and keeps
the plan, journal, and accepted history. If the browser asks for login, sign
in normally. If it asks for technical verification, follow the requested
Human verification; the diagnostic will identify the next action. See
[`docs/browser-lifecycle.md`](docs/browser-lifecycle.md) for the detailed
boundary.

## Common errors

| Symptom | Next action |
| --- | --- |
| `workflow.exe` command not found | Run `.\install.ps1`, open a new PowerShell, and confirm `Get-Command workflow.exe`. |
| `THIS_IS_WORKFLOW_ENGINE_REPOSITORY` | Change to the Business Project directory; never start business work in the Engine checkout. |
| `PLAN_INPUT_MISSING` | Create both exact files: `plan/REQUIREMENTS.md` and `plan/STAGE_PLAN.md`. |
| `MISSING_CANONICAL_PLAN_FILE` | Rename the human source to the exact expected name; Workflow will not rename it. |
| `AMBIGUOUS_PLAN_SOURCE` | Pick one authoritative source and remove the numbered-name ambiguity. |
| `SETUP_REQUIRED` | Run `workflow.exe setup` from a fresh PowerShell. |
| Codex not logged in | Run `codex login`, then rerun `workflow.exe doctor --probe-browser`. |
| ChatGPT browser not logged in | Sign in in the machine-local Browser Bridge profile, then rerun the probe. |
| `BROWSER_HUMAN_VERIFICATION_REQUIRED` | Complete the requested browser verification, then rerun `workflow.exe resume`. |
| Project ChatGPT URL missing | Add the intended project URL only in the `Workflow Binding` section of `REQUIREMENTS.md`, then resume. |
| Browser was closed | Run `workflow.exe resume`; the project profile is recovered automatically. |
| `INSTALLED_ENGINE_OUTDATED` | Reinstall from the checkout you intend to use with `.\install.ps1`, then open a new PowerShell. |
| `MCP_REGISTRATION_MISMATCH` or stale MCP | Run `workflow.exe setup` to refresh the registration, then rerun doctor. |

`workflow.exe doctor --probe-browser` is the troubleshooting entry point. For
each failure it reports WHAT FAILED, WHY, and WHAT TO DO NEXT, including
Python, Node, Codex CLI/auth, Browser Bridge version/digest, browser profile
and login, MCP registration, runtime config, launcher PATH, Engine provenance,
and project binding.

## Documentation

- [`docs/installation.md`](docs/installation.md) — prerequisites and setup details
- [`docs/quickstart.md`](docs/quickstart.md) — short project-start reference
- [`docs/project-planning.md`](docs/project-planning.md) — two-file plan contract
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — diagnostic codes and recovery
- [`docs/architecture.md`](docs/architecture.md) — Engine/project/machine ownership
- [`docs/browser-lifecycle.md`](docs/browser-lifecycle.md) — per-project browser boundary

## Development

The reusable source is under `src/`, `bridge/`, `schemas/`, and `scripts/`.
Run the Python and Bridge tests from the Engine checkout. Historical release
evidence is kept under `docs/history/` and is not part of the product startup
path.
