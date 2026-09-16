# Release notes

## v2.1.7

This patch closes the public GitHub productization and Windows installation
boundary:

- cleaned the public repository root and separated user docs from historical
  development evidence;
- rewrote the first-run README around Engine vs Business Project;
- made `plan/REQUIREMENTS.md` + `plan/STAGE_PLAN.md` the default formal entry;
- added stable `%LOCALAPPDATA%\ResearchWorkflow\bin\workflow.exe` launcher and
  persistent user PATH setup;
- added `workflow.exe --version` provenance and doctor detection for stale
  installs, launcher shadowing, Bridge version/digest, and actionable failures;
- added fail-closed diagnostics for misspelled and ambiguous plan sources.

## v2.1.6

The prior stable release provided:

- two-file Plan Ingestion;
- autonomous objective continuation;
- per-project ChatGPT binding;
- persistent per-project Browser Bridge/profile;
- browser restart recovery;
- automatic technical GPT escalation application;
- clean-room/facade real E2E closure.

## Authentication boundary

Codex and ChatGPT authentication remain machine-local. Use the official
`codex login` flow and sign in normally in the persistent Browser Bridge
profile. Cookies, tokens, passwords, browser profiles, and private project
state are never release assets.
