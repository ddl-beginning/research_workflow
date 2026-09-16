# Browser lifecycle

The Browser Bridge is a machine-local runtime dependency. The Engine ships
its source and lockfile; `workflow.exe setup` provisions one copy under
`%LOCALAPPDATA%\ResearchWorkflow\bridge`.

Each Business Project has one persistent project browser/profile. The optional
ChatGPT Project URL is a project binding, not a credential. Browser state and
authentication stay on the machine that created them.

## First login

1. Run `workflow.exe setup` from the Engine checkout.
2. Open a new PowerShell and run `workflow.exe doctor --probe-browser`.
3. When the Browser window appears, sign in to ChatGPT normally.
4. Rerun the probe, then resume from the Business Project directory.

Do not copy cookies, tokens, passwords, local storage, or the browser profile.
Do not put `.auth/`, browser state, or machine configuration in Git.

## Recovery

If a browser process is closed or crashes, run `workflow.exe resume` from the
same Business Project. The Bridge reuses or rebuilds the same project-specific
profile and the Workflow resumes from canonical state. It does not re-ingest a
completed plan as a new project, ask for a new project URL, or require manual
journal/receipt creation.

Human action is limited to authentication or an explicit browser verification
step. Technical browser restart and continuation are handled by Workflow.

## Diagnostics

`workflow.exe doctor --probe-browser` reports whether the Bridge source,
version, digest, Node runtime, profile, and login probe are healthy. A failure
is bounded and includes the next action. `GPT_AUTH_REQUIRED` means normal
login is needed. `BROWSER_HUMAN_VERIFICATION_REQUIRED` means complete the
visible verification and rerun the command.
