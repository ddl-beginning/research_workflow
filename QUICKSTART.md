# Workflow V2 Quickstart

This is the short path for a machine that has already completed
[INSTALL.md](INSTALL.md).

## 1. Select or create a project

Use an existing Git project or create an empty directory. The project must be
different from the Product Engine checkout.

```powershell
$project = 'C:\work\workflow-v2-release-smoke'
New-Item -ItemType Directory -Force $project | Out-Null
Set-Location $project
git init
```

## 2. Initialize the Product runtime

```powershell
python C:\path\to\workflow-v2-product\scripts\research_workflow_cli.py init `
  --workspace $project `
  --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"
```

This creates only the project brief/runtime prerequisites when the machine is
ready. It does not create a Stage or call GPT.

## 3. Use the MCP workflow

In the Codex session connected to `research-supervisor`, use this sequence:

```text
workflow_resume(workspace=PROJECT)
workflow_start(workspace=PROJECT, brief=CONFIRMED_REQUIREMENT)
workflow_answer(workspace=PROJECT, approve=true)
workflow_run(workspace=PROJECT, request=PLAN_STAGE)
workflow_run(workspace=PROJECT, request=START)
workflow_run(workspace=PROJECT, request=REQUEST_EXECUTION)
workflow_run(workspace=PROJECT, request=RECORD_OBSERVATION)
workflow_run(workspace=PROJECT, request=ASSESS_RESULT)
workflow_run(workspace=PROJECT, request=CONSULT_REVIEW)
workflow_run(workspace=PROJECT, request=STAGE_READY / COMMIT_INTEGRATION / CLOSEOUT)
```

The MCP server and controller supply the exact subject IDs, digests, and next
commands. Do not invent a second brief, Stage, operation, or receipt. Ask the
current intake question one at a time when `workflow_resume` returns one.

## 4. Read the result

```powershell
Get-Content .research\PROJECT_BRIEF.json
Get-ChildItem specs -Recurse
Get-Content specs\<stage-id>\spec.md
Get-Content specs\<stage-id>\plan.md
Get-Content specs\<stage-id>\tasks.md
Get-Content specs\<stage-id>\CLOSEOUT.md
python C:\path\to\workflow-v2-product\scripts\product_doctor.py `
  --workspace $project `
  --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"
```

`workflow_resume` is safe to call after a process restart. It re-loads the
same project workspace and V2 journal; it does not replay an unresolved
external operation. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for resume
and identity blockers.

