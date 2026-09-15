# Workflow V2.1 Quickstart

The machine must have completed [INSTALL.md](INSTALL.md) first. A release
checkout includes the Browser Bridge; setup provisions it automatically.

## Another computer

```powershell
git clone --branch workflow-v2.1.2-stable <PRODUCT_REPOSITORY_URL> C:\work\workflow-v2-product
Set-Location C:\work\workflow-v2-product
.\install.ps1
workflow.exe setup
workflow.exe doctor --probe-browser
```

Sign in normally to Codex and, if requested, to ChatGPT Web in the visible
dedicated browser profile. Do not copy cookies, profile files, tokens, or
runtime config.

## New project

```powershell
Set-Location C:\work\my-project
workflow.exe init --goal "Describe the project goal"
```

This binds one canonical project brief and the default
`autonomous_research` execution profile. It does not create or start a Stage.

## Existing project

```powershell
Set-Location C:\work\my-project
workflow.exe
```

The no-argument command resumes when `.research\PROJECT_BRIEF.json` is
present. The explicit fallback is:

```powershell
workflow.exe resume
```

The optional `$workflow` Codex trigger delegates to the same command. If it is
unavailable, use `workflow.exe` (or `research-workflow`); no slash command is
required. In PowerShell, the bare `workflow` name is reserved and should not
be used.

At `STAGE_READY`, `HUMAN_GATE`, or `BLOCKED`, the first screen is a plain
language Human Summary. The stable `MACHINE_DETAILS` block follows it. A
visual gate without a real PNG/HTML/SVG review artifact is reported as
`HUMAN_REVIEW_PRESENTATION_INCOMPLETE` and enters presentation recovery.

## Readiness and auth

```powershell
workflow.exe doctor
workflow.exe doctor --probe-browser
```

If Codex auth is required, run `codex login`. If GPT Browser auth is required,
sign in normally in the dedicated headed browser profile and rerun setup or
the probe. Never copy or export cookies, browser storage, passwords, or
tokens.
