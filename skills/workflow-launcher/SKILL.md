---
name: workflow
description: Start or resume the installed Workflow project from the current directory.
metadata:
  short-description: Thin Workflow startup launcher
---

When the user explicitly invokes `$workflow` or asks to start or resume
Workflow, run the installed canonical `workflow` command from the current
working directory (`workflow.exe` in Windows PowerShell). Use `workflow resume`
for an existing project; use `workflow init` only when the user explicitly
wants to initialize a new project. Surface the command's result and any
requested human authentication step.

This skill is only a launcher. Do not reproduce Workflow lifecycle rules,
research policy, retry behavior, model routing, review semantics, or browser
authentication logic here.
