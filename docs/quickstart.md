# Quickstart

After installing the Engine, put the two human-owned plan files in a Business
Project:

```text
my_project/
└─ plan/
   ├─ REQUIREMENTS.md
   └─ STAGE_PLAN.md
```

Then run:

```powershell
Set-Location D:\work\my_project
workflow.exe resume
```

This is the canonical startup command for both a new two-file project and an
existing project. Workflow creates or resolves the project identity, ingests
the plan, writes `plan/WORKFLOW_PLAN.md` and `plan/CURRENT_STATE.md`, and
resumes the next legal Stage.

The supported no-argument shortcut is `workflow.exe`; when the two source
files are present it resolves to the same `resume` operation. `workflow.exe
init --goal "..."` remains available only as a manual/legacy intake path when
you do not have the two-file plan.

Useful checks:

```powershell
workflow.exe status
workflow.exe doctor --probe-browser
workflow.exe resume
```

The Engine checkout and Business Project must be different directories. See
[`../README.md`](../README.md) and [`architecture.md`](architecture.md).

`$workflow` is an optional thin Codex launcher for the same canonical
`workflow resume` command; the installed `workflow.exe` command remains the
portable entrypoint.
