# Workflow V2 Product

Workflow V2 Product is a local, bounded workflow runner for turning a new
project requirement into reviewable specifications, a planned Stage, a real
Provider execution, technical review, resumable integration, and a canonical
closeout. The V2 lifecycle is owned by one durable `StageController` journal.

The Product is deliberately split into four ownership boundaries:

```text
Product Engine (this repository)
  → Project workspace (.research, specs, stage evidence)
  → Machine-local runtime (%LOCALAPPDATA%/ResearchWorkflow)
  → External services (Codex CLI, ChatGPT auth, Browser Bridge)
```

The engine does not copy credentials, does not silently fall back to the
legacy V1 runtime, and does not run an autonomous loop. `STAGE_READY` is a
technical review result; integration and closeout remain explicit controller
commands.

## Start here

- New machine: [INSTALL.md](INSTALL.md)
- Daily use: [QUICKSTART.md](QUICKSTART.md)
- Failures and recovery: [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Release evidence: [RELEASE_VALIDATION.md](RELEASE_VALIDATION.md), when present
- Product architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
- Release scope and limitations: [RELEASE_NOTES.md](RELEASE_NOTES.md)

## Prerequisites

- Windows 10/11 with Python 3.11 or newer
- Node.js 20 or newer for the separately installed Browser Bridge
- Git
- Codex CLI with a saved ChatGPT login
- An authenticated headed Browser Bridge profile for real GPT planning and review

Runtime Python code uses only the standard library. `requirements-dev.txt`
contains the optional pytest development dependency.

## Install and diagnose

From a checkout of the release candidate:

```powershell
python -m pip install -e .
python scripts/product_doctor.py --workspace C:\path\to\project --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"
```

The doctor is read-only. It checks the Product files, project workspace,
Python, Node, explicit V2 config, Codex/ChatGPT availability, exact standard
model `gpt-5.6-luna`, Browser Bridge, machine-local runtime policy, and the
registered Product MCP entry. It never creates a Stage, dispatches a Provider,
calls GPT, or writes workflow state.

## Quick architecture

```text
workflow_start / workflow_answer
          ↓ approved project brief
workflow_run PLAN_STAGE  ── real GPT planning consultation
          ↓ PLANNED Stage in .workflow-v2/journal.json
START → REQUEST_EXECUTION → real Luna/max Provider
          ↓ immutable Observation + Assessment
CONSULT_REVIEW ─────────── real GPT Technical Review
          ↓ STAGE_READY
COMMIT_INTEGRATION → APPLY_RECEIPT → CLOSEOUT → CLOSEOUT.md
```

Project artifacts stay in the project workspace. Engine files stay in this
repository. Temporary upload/context material is machine-local and
regenerable. The old V1 host and frozen V2 checkout remain read-only and are
not dependencies of a new project.

## Common entry points

```powershell
# Install the Python entry points in the current environment.
python -m pip install -e .

# Initialize one existing project after machine config is ready.
python scripts/research_workflow_cli.py init `
  --workspace C:\path\to\project `
  --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"

# Inspect the Product install and project readiness.
python scripts/product_doctor.py --workspace C:\path\to\project `
  --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"
```

The normal workflow commands are exposed through the registered
`research-supervisor` MCP server: `workflow_resume`, `workflow_start`,
`workflow_answer`, `workflow_run`, and `workflow_status`. Always resume first.

## Project artifact locations

- `.research/PROJECT_BRIEF.json`: one project identity and approved brief
- `.workflow-v2/journal.json`: canonical V2 runtime journal, ignored by Git
- `.research/planning/`: planning intent and validated consultation receipt
- `.research/reviews/`: technical-review intent and receipt
- `specs/<stage-id>/`: project constitution/spec/plan/tasks/closeout artifacts
- `.research/integration/` and `.research/verification/`: durable evidence
- `%LOCALAPPDATA%\ResearchWorkflow\`: machine-local temporary runtime data

Do not put tokens, cookies, browser profiles, or API keys in the repository or
in runtime configuration. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when a
doctor check or workflow gate fails.

