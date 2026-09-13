# Workflow V2 Product Installation

This guide starts from a new Windows machine and a clean Product checkout.
Use a separate directory for the Product Engine and each project. All paths
below are examples; replace them with paths on the current machine.

## Prerequisites: required versus optional

| Item | Required for | Ownership |
| --- | --- | --- |
| Python 3.11+ and Git | Every operation | Machine-local installation |
| Node.js 20+ | Browser Bridge and real GPT | Machine-local installation |
| Codex CLI | Provider execution and MCP registration | Machine-local installation |
| Saved ChatGPT login | Real Luna/max Provider and GPT consultation | Secret/account state; never copied |
| Browser Bridge checkout and `npm ci` | Real planning/review consultation | External dependency; separate checkout |
| Browser Bridge headed profile | Real GPT consultation | Secret/browser state; never committed |
| Product repository | All Product commands | Product Engine |
| Project checkout | `init`, intake, Stage work | Project-specific |
| ChatGPT Project URL | Optional project-scoped consultation | Project-specific, non-secret |
| `requirements-dev.txt` | Running Product tests | Optional development dependency |

## 1. Clone the Product

```powershell
git clone --branch workflow-v2-product-rc1 <PRODUCT_REPOSITORY_URL> C:\work\workflow-v2-product
Set-Location C:\work\workflow-v2-product
git status --short --branch
```

If the release candidate uses another tag, substitute the exact tag recorded
in `RELEASE_VALIDATION.md`.

## 2. Install Python dependencies

The runtime has no third-party Python dependency. An editable install keeps
the repository's schemas/templates beside the imported source:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

For development and regression tests:

```powershell
python -m pip install -r requirements-dev.txt
```

## 3. Install and configure Codex CLI

Install the current Codex CLI using the official OpenAI Codex setup
instructions for the machine, then verify the executable:

```powershell
codex --version
codex login status
```

The Product requires the saved ChatGPT auth mode. If the status is logged out,
run `codex login` and finish the browser/device flow in the Codex UI. Do not
paste passwords, tokens, cookies, or access codes into a terminal or this
repository. The Product doctor checks only presence and mode.

## 4. Install the Browser Bridge

The bridge is a separate dependency and is not copied into this repository:

```powershell
git clone <BROWSER_BRIDGE_REPOSITORY_URL> C:\work\chatgpt_browser_bridge
Set-Location C:\work\chatgpt_browser_bridge
npm ci
```

Create or select its dedicated headed profile. On the first real consultation,
the visible browser may require manual ChatGPT login. The bridge must be able
to read its own `scripts\consult-pack.mjs`; never pass the profile directory
as an attachment or commit it.

## 5. Create machine-local V2 configuration

Create the directory and file outside both Git repositories:

```powershell
$machineRoot = Join-Path $env:LOCALAPPDATA 'ResearchWorkflow'
New-Item -ItemType Directory -Force $machineRoot | Out-Null
$config = Join-Path $machineRoot 'product-v2-runtime.json'
```

The accepted machine contract is the non-secret `runtime-composition.v2`
Product contract: explicit V2 lifecycle, ChatGPT auth, Luna standard routing,
and a separately owned Browser Bridge.

Write a non-secret configuration with the actual dependency paths. For
example:

```json
{
  "schema_version": "runtime_composition.v1",
  "lifecycle_version": "v2",
  "stage": { "state_path": ".workflow-v2/journal.json" },
  "executor": {
    "providers": ["openai-codex"],
    "preferred_model": "gpt-5.6-luna",
    "fallback_model": null,
    "auth_mode": "chatgpt",
    "timeout_seconds": 600
  },
  "bridge": {
    "enabled": true,
    "root": "C:/work/chatgpt_browser_bridge",
    "profile_dir": "C:/work/chatgpt_browser_bridge/.auth/chatgpt-profile",
    "node_executable": "node",
    "transport": "homepage_fallback",
    "project_url": null
  }
}
```

Use forward slashes or escaped backslashes in JSON. Do not add `token`,
`cookie`, `secret`, `password`, `api_key`, or `auth_token` fields. Temporary
resolver data defaults to `%LOCALAPPDATA%\ResearchWorkflow`; it is not a Git
authority and is safe to regenerate.

## 6. Register the Product MCP entry

Register the Product launcher with the same machine-local config path:

```powershell
$product = 'C:\work\workflow-v2-product'
$python = (Get-Command python).Source
codex mcp add research-supervisor `
  --env "RESEARCH_WORKFLOW_RUNTIME_CONFIG=$config" `
  -- $python "$product\scripts\workflow_mcp.py"
```

If an old `research-supervisor` entry exists, inspect it first with
`codex mcp list --json`. Remove/replace only that named Product entry, then
reload the Codex session so the new MCP process is loaded. The registration
must point to `scripts/workflow_mcp.py`, not the frozen V1 host.

## 7. Run the doctor and initialize a project

```powershell
Set-Location $product
python scripts/product_doctor.py --workspace C:\work\my-project --runtime-config $config
python scripts/research_workflow_cli.py init --workspace C:\work\my-project --runtime-config $config
```

The doctor must report `ready: true`. It performs no Stage creation, Provider
dispatch, GPT call, or workflow write. `init` may create the project-local
brief/runtime prerequisites only after the doctor passes; it still creates no
Stage.

The installed console entry point is also available as
`workflow-v2-doctor --workspace <project> --runtime-config <config>`.

## 8. Verify the real consultation path

Run the short smoke only when a real headed consultation is intended:

```powershell
Set-Location C:\work\chatgpt_browser_bridge
npm run consult -- --prompt "Reply with exactly: BRIDGE_OK"
```

The bridge receipt must show one request, a validated conversation identity,
and a bounded packet digest. A `LOGIN_REQUIRED` result is an actionable setup
state, not permission to bypass authentication.

## 9. Verification checklist

- `codex login status` reports ChatGPT auth
- `codex debug models` lists `gpt-5.6-luna`
- `codex mcp list --json` contains `research-supervisor` and the Product launcher
- `product_doctor.py` reports `ready: true`
- Product `workflow_resume` sees the selected project
- a new project gets a new `PROJECT_BRIEF.json` and `.workflow-v2/journal.json`
- no old `.research`, `.workflow-v2`, consultation, or browser-profile data was copied

For a failed check, stop and use [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
