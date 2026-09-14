# Workflow V2.1

Workflow is a local, resumable product runner for bounded research and
engineering projects. It keeps the project brief and V2 journal in the project
directory, and keeps machine-specific runtime settings outside Git.

## Install

On Windows, from a clean checkout:

```powershell
.\install.ps1
workflow.exe setup
```

The checkout contains the versioned Browser Bridge source. `workflow.exe setup`
provisions its npm dependencies into `%LOCALAPPDATA%\ResearchWorkflow\bridge`;
no separate Bridge checkout or manual copy is required. Workflow runtime code
uses the Python standard library. `requirements-dev.txt` is only for tests.

The equivalent packaging command is:

```powershell
python -m pip install -e .
workflow.exe setup
```

`workflow.exe setup` checks Python, Node.js, Codex, the saved ChatGPT login, the
packaged Browser Bridge version, its npm dependencies, the machine-local
runtime config, the dedicated browser profile, MCP registration, and the final
doctor report. It may ask you to complete a normal browser login. It never
reads or exports browser cookies.

In Command Prompt, `workflow` is the short command. Windows PowerShell
reserves the bare word `workflow`, so use `workflow.exe` or the equivalent
`research-workflow` alias there.

## Start and resume

From a new project directory:

```powershell
workflow.exe init --goal "Describe the project goal"
```

Review and approve the resulting `.research\PROJECT_BRIEF.json` when the
product asks for it. For an existing initialized project:

```powershell
workflow.exe
```

The no-argument command resumes the current project when one is present. In a
directory without a project identity it prints the smallest safe init
guidance and does not initialize anything automatically. The canonical
fallback is always:

```powershell
workflow.exe resume
```

The optional Codex convenience trigger is `$workflow`. It is only a thin
launcher for the canonical CLI; the CLI remains the portable, supported
entrypoint.

When a Stage reaches a reviewable checkpoint, the launcher prints a Human
Summary first: conclusion, completed work, visual focus, remaining unknowns,
required user action, and next step. Stable `MACHINE_DETAILS` follow as the
parser-facing second layer. `CONTINUE` and `REPLAN` remain automatic.

The two formal Codex routes are `STANDARD` (`gpt-5.6-luna`, `max`) and
`FRONTIER` (`gpt-6-astra`, `low`). The binding and subprocess audit are in
`MODEL_ROUTING_AUDIT.md`.

## First-run authentication

Codex uses its official `Sign in with ChatGPT` flow. If required, sign in with
`codex login`, then rerun `workflow.exe setup`.

The Browser Bridge uses a separate persistent browser profile owned by the
machine. Sign in interactively in that profile once. If a session expires,
run:

```powershell
workflow.exe doctor --probe-browser
```

Never export, copy, paste, encode, or commit cookies, browser storage,
passwords, access tokens, or session tokens. Authentication is intentionally
machine-local and must be completed normally on a different computer. The
Bridge source and dependencies are portable; the browser profile is not.

## Useful commands

```powershell
workflow.exe setup
workflow.exe doctor
workflow.exe init --goal "..."
workflow.exe resume
workflow.exe status
workflow.exe setup --reset-machine-config
```

`workflow.exe setup --reset-machine-config` removes only the generated machine
runtime config. It preserves project briefs, journals, history, and browser
profile state.

## Ownership and advanced architecture

```text
Workflow Engine (this repository)
  → Versioned Browser Bridge source (bridge/)
  → Project workspace (.research/PROJECT_BRIEF.json, .workflow-v2/journal.json)
  → Machine-local runtime (%LOCALAPPDATA%/ResearchWorkflow)
  → External services (Codex CLI, ChatGPT Web)
```

The existing V2 `StageController` remains the sole lifecycle authority. The
`autonomous_research` execution profile is a durable policy projection stored
in the canonical project brief; it does not add a controller or recovery
lifecycle. Detailed contracts and release evidence are documented in
`ARCHITECTURE.md`, `RELEASE_NOTES.md`, and `RELEASE_VALIDATION.md` when those
files are present.

For symptoms and safe recovery, read [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
