# Windows installation

This guide installs the Workflow Engine on one computer. It does not install
or copy a Business Project.

## Prerequisites

- Windows PowerShell
- Python 3.11 or newer
- Node.js and npm
- Codex CLI, installed through its official distribution

Check the prerequisites with `python --version`, `node --version`, `npm
--version`, and `codex --version`.

## Install

```powershell
git clone https://github.com/ddl-beginning/research_workflow.git
Set-Location research_workflow
git checkout workflow-v2.1.7-stable
.\install.ps1
```

The script performs an editable install from this checkout, copies the
generated console launcher to `%LOCALAPPDATA%\ResearchWorkflow\bin\workflow.exe`,
and persists that directory in the Windows user PATH. The current shell is
updated for convenience, but the supported validation is a new PowerShell:

```powershell
$fresh = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Get-Command workflow.exe; workflow.exe --version' -Wait -PassThru
if ($fresh.ExitCode -ne 0) { throw 'Fresh shell validation failed.' }
```

The standard short form is:

```powershell
Get-Command workflow.exe
workflow.exe --version
```

The command must resolve to the stable launcher directory. Do not add a
Python `Scripts` directory manually.

## Setup and doctor

From the Engine checkout, run:

```powershell
workflow.exe setup
workflow.exe doctor --probe-browser
```

Setup provisions the versioned `bridge/` source and lockfile dependencies into
machine-local storage, writes non-secret runtime configuration, installs the
thin Workflow skill, and refreshes the `research-supervisor` MCP registration.
It does not copy project data or credentials.

If setup reports `CODEX_AUTH_REQUIRED` or `GPT_AUTH_REQUIRED`, use the normal
ChatGPT/Codex interactive login flow, then rerun setup/doctor. No cookies or
tokens need to be supplied to Workflow.

## Machine-local files

The following remain outside the Git checkout:

```text
%LOCALAPPDATA%\ResearchWorkflow\
├─ bin\workflow.exe
├─ bridge\
├─ browser-profile\
├─ product-v2-runtime.json
├─ runtime\
├─ workspace-registry.json
└─ health.json
```

Codex's own login storage stays under its official user configuration. The
Browser Bridge profile is persistent per computer and must not be copied to a
second computer.

Do not paste cookies, tokens, passwords, or browser storage into a terminal or
configuration file.
