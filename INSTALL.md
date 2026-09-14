# Install Workflow V2.1

This is the complete first-machine path for Windows 10/11.

## Prerequisites

- Python 3.11 or newer
- Git
- Node.js 20 or newer for Browser Bridge
- Codex CLI
- A separate Browser Bridge checkout

Workflow runtime code uses the Python standard library. `requirements-dev.txt`
is only for tests.

## Install the engine

```powershell
git clone --branch workflow-v2.1.1-stable <PRODUCT_REPOSITORY_URL> C:\work\workflow-v2-product
Set-Location C:\work\workflow-v2-product
.\install.ps1
```

The equivalent command is `python -m pip install -e .`. The package exposes
both `workflow` and `research-workflow` console commands.

## Browser Bridge provisioning

The release checkout already contains the versioned Bridge source under
`bridge/`. You do not clone, copy, or configure another Bridge directory.
`workflow.exe setup` copies only the source/package files to the machine-local
provision root, runs `npm ci`, verifies the Bridge version and source digest,
and creates the runtime binding. `node_modules` is never committed.

## First-run setup

```powershell
Set-Location C:\work\workflow-v2-product
workflow.exe setup
```

Setup creates the non-secret machine config at
`%LOCALAPPDATA%\ResearchWorkflow\product-v2-runtime.json`, a regenerable
workspace registry, and a dedicated browser profile at
`%LOCALAPPDATA%\ResearchWorkflow\browser-profile` when no explicit profile is
already configured. It provisions the packaged Browser Bridge, registers the
Product MCP entry, and installs the thin `workflow` launcher skill in the Codex
user skill directory.

If setup reports `AUTH_REQUIRED`, use the official flows:

```powershell
codex login
workflow.exe setup
```

For GPT Browser `LOGIN_REQUIRED`, keep the headed browser visible, sign in to
ChatGPT normally, close or finish the browser flow, and rerun `workflow.exe setup`.
Do not paste passwords or one-time codes into a terminal. Do not read, export,
copy, or commit cookies or browser storage.

## Verify

```powershell
workflow.exe doctor
workflow.exe doctor --probe-browser
```

The default doctor is written for people. Add `--verbose --json` when a
bounded machine-readable report is useful. It never prints credentials or
raw subprocess output.

The execution route is selected by the existing bounded policy: STANDARD uses
Luna Max and FRONTIER uses Astra Low. The parent Codex session is not changed;
the selected model is passed to the isolated Codex execution seam. See
`MODEL_ROUTING_AUDIT.md` for the local capability audit.

## Start a project

```powershell
Set-Location C:\work\my-project
workflow.exe init --goal "Describe the project goal"
```

Review the canonical `.research\PROJECT_BRIEF.json` and approve it through the
existing Product flow. `workflow.exe init` creates no Stage. A later invocation of
`workflow.exe` or `workflow.exe resume` restores the same project identity, V2 journal,
and `autonomous_research` profile.

## Reset

```powershell
workflow.exe setup --reset-machine-config
```

This removes only the machine-local runtime config. It does not delete a
project brief, journal, history, browser profile, or Codex login. Authentication
must be performed independently on each computer; engine portability never
means copying authentication state. Never copy cookies, tokens, or a browser
profile.
