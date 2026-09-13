# Workflow V2 Troubleshooting

Use the symptom, meaning, and safe recovery below. Do not delete or rewrite a
journal, receipt, consultation, quarantine item, or browser profile to make a
check pass.

## RUNNER_NOT_CONFIGURED / CODEX_RUNTIME_UNAVAILABLE

Meaning: the Product machine contract is missing or not explicitly V2.

Safe diagnosis:

```powershell
python scripts/product_doctor.py --workspace C:\path\to\project --runtime-config "$env:LOCALAPPDATA\ResearchWorkflow\product-v2-runtime.json"
```

Safe recovery: create/fix the machine-local JSON, especially
`lifecycle_version: "v2"`, then rerun the doctor. Human action is required
when choosing dependency locations or approving a new MCP registration.

## ChatGPT/Codex authentication missing (AUTHENTICATION_REQUIRED)

Meaning: Codex is not using a saved ChatGPT session, or the headed browser
profile is logged out.

Safe diagnosis: run `codex login status`, then run one explicit bridge smoke.
Safe recovery: use `codex login` or the visible browser's manual login flow.
Never put the credential, cookie, token, or one-time code in a file or prompt.
Human action is required for account authentication.

## Browser Bridge unavailable (BRIDGE_CONFIGURATION_REQUIRED / STANDARD_MODEL_UNAVAILABLE)

Meaning: `scripts/consult-pack.mjs`, Node, profile, or the explicit transport
scope cannot be found.

Safe diagnosis: check the configured `bridge.root`, `profile_dir`,
`node_executable`, and `transport`; run `node --version` and
`Test-Path <bridge>\scripts\consult-pack.mjs`.

Safe recovery: run `npm ci` in the separate bridge checkout and repair the
machine-local paths. Do not copy the bridge profile into the Product or
project. Human action is required if the browser needs login or a project URL
must be selected.

## ATTACHMENT_NOT_READY

Meaning: a declared attachment was not ready in the browser, so the prompt was
not sent (`request_count=0`).

Safe diagnosis: inspect the bounded bridge receipt and confirm the attachment
path, size, and upload status. Do not retry blindly when the receipt says a
request effect is unresolved.

Safe recovery: fix the local attachment or browser composer state, create a
deliberately revised packet, and keep the failed receipt. Human action is
required if the external effect cannot be proven.

## Conversation or project identity mismatch (MCP_NOT_REGISTERED / WORKFLOW_NOT_FOUND)

Meaning: a receipt, ChatGPT Project URL, conversation, or selected workspace
does not bind to the current request.

Safe diagnosis: compare only the bounded consultation/project/workspace IDs
and packet digest in the receipt and current Product journal.

Safe recovery: select the intended project explicitly and start a fresh
consultation from a fresh packet. Never edit a receipt or substitute an old
conversation. Human action is required when two valid identities remain.

## Workspace identity mismatch (HUMAN_APPROVAL_REQUIRED)

Meaning: the request targets a different project directory than the one bound
to the current brief or journal.

Safe diagnosis: run `workflow_resume` with the exact project path and inspect
the returned `project_id`, `workspace_id`, and current Stage.

Safe recovery: return to the bound workspace, or intentionally start a new
project with a new identity. Do not copy old runtime state into the new path.

## Wrong Product or legacy V1 entry

Meaning: MCP is loading the frozen V1 host or a stale launcher process.

Safe diagnosis: run `codex mcp list --json` and verify the target command is the
current Product `scripts/workflow_mcp.py`; then rerun the Product doctor and
reload the Codex session.

Safe recovery: replace only the named `research-supervisor` registration using
the command in [INSTALL.md](INSTALL.md). Never modify the read-only V1 host or
frozen V2 checkout to repair a Product installation.

## Missing machine-local runtime config

Meaning: the project has no explicit non-secret composition file, or the path
in `RESEARCH_WORKFLOW_RUNTIME_CONFIG` is stale.

Safe diagnosis: check `Test-Path $env:LOCALAPPDATA\ResearchWorkflow` and rerun
doctor with `--runtime-config` explicitly.

Safe recovery: recreate the JSON from the documented shape, using actual local
paths and no secrets. Human action is required for dependency-path selection.

## MCP process using stale registration

Meaning: `codex mcp list` is correct but the currently loaded Codex/MCP child
still belongs to an earlier registration.

Safe diagnosis: inspect a new `workflow_resume` response and, if enabled, the
metadata-only `RESEARCH_WORKFLOW_MCP_TRACE_PATH` trace.

Safe recovery: reload/restart the Codex session after registration changes.
Do not send workflow mutations to both old and new launchers.

## Unknown external effect / resume blocker (CONSULTATION_EFFECT_UNRESOLVED)

Meaning: a Provider, integration, or bridge operation may have happened but a
settled receipt is unavailable. The V2 controller intentionally blocks replay,
assessment, or closeout until the effect is resolved.

Safe diagnosis: resume the exact project and inspect the operation ID, effect
state, blocker, and existing receipt. Preserve all immutable evidence.

Safe recovery: obtain an authoritative settled receipt or a controller-backed
proof that the effect was not sent, then apply that receipt once. Human action
is required when the external system cannot identify the operation.
