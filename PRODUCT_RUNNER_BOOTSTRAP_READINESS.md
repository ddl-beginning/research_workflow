# Product Runner Bootstrap Readiness

Date: 2026-09-12. Branch: `workflow-v2-product-architecture`.
Baseline: `9203147ec2db6f990500c1995cffc61fbba2d595`.

`RUNNER_BOOTSTRAP_READINESS: PASS`.
`canonical workflow_run: PASS` through the actual Product STDIO MCP launcher.
The real GPT planning response was CONTINUE and registered the approved Stage through the frozen V2 controller. No old runtime was copied; no lifecycle kernel was changed.

## Root cause and evidence

The original machine MCP registration loaded `research_supervisor_poc/scripts/workflow_mcp.py`. Passing a different workspace selected a data root, not a different implementation. The baseline Product launcher also defaulted to the legacy `WorkflowRuntime` / Core V1 composition.

The verified failure chain was `workflow_run` → `WorkflowRuntime.run` → `_run_orchestrator_prepare` → `build_workflow_orchestrator` → `_configured_bridge_runner`. With no injected consultant and `bridge.enabled=false`, that builder raised `RUNTIME_COMPOSITION_NOT_READY`, missing `bootstrap.consultant / CONSULTANT_REQUIRED`, with message `Bootstrap orchestrator is not ready`. `_orchestrator_failure` translated it to `RUNNER_NOT_CONFIGURED`; existing config details masked the missing-component list in the public response. The actual old MCP response remains in `PRODUCT_RUNNER_CANONICAL_FAILURE.json`.

The V2 lifecycle candidate passed Phase 7 because its validation harness explicitly imported `workflow_v2_controller.StageController` and supplied provider/bridge configuration. That demonstrated kernel behavior, not public MCP wiring. Neither old Stage receipts nor an old project identity were the missing prerequisite.

Enabling the V1 bridge alone would activate the wrong lifecycle path. The implemented repair is an explicit composition adapter selecting the already-existing V2 authority. It adds no bootstrap Stage, lifecycle state, reducer, or retry semantics. The earlier diagnostic concern about activating V1 as V2 is resolved by keeping that path separate; no lifecycle architecture deviation was implemented.

## Configuration ownership

| Dependency | Owner | Bootstrap treatment |
|---|---|---|
| Product launcher, V2 adapter, reusable CLI, runtime config schema | ENGINE | Shipped as product files; explicit `lifecycle_version: v2` selects Product entry |
| Frozen V2 controller/contracts/runtime/schema | ENGINE | Existing single lifecycle authority, unchanged |
| Runtime schema/defaults, provider registry, routing rules | ENGINE | Reuse `runtime_composition.v1`; no second configuration system |
| Python, Node, Codex CLI, Browser Bridge installation, MCP registration | MACHINE_LOCAL | Install/discover normally; configure locations externally |
| External runtime-config filename and nonsecret bridge/profile locations | MACHINE_LOCAL | Explicit `--runtime-config` or `RESEARCH_WORKFLOW_RUNTIME_CONFIG` |
| Codex model availability | MACHINE_LOCAL | Existing provider discovery; exact `gpt-5.6-luna`, no fallback |
| Saved Codex ChatGPT login and browser profile credentials | SECRET | Normal existing discovery; no token/cookie copying or printing |
| Workspace root, project identity, approved PROJECT_BRIEF, optional ChatGPT Project binding | PROJECT | Supplied/initialized for the current project; no old identity import |
| Guidance and ActionMap/executable request | PROJECT | Work inputs, not initialization prerequisites; not synthesized to satisfy doctor |
| Journal, Stage/attempt/observation/assessment and consultation receipts | RUNTIME | Created only by their operation/authority after bootstrap; never prerequisite copies |
| Accepted scope, final review and durable audit evidence | PROJECT | Retained with the owning project after creation |
| Upload staging, scratch/cache | RUNTIME | Regenerable operation output; not runner prerequisites |

## Files and configuration changed

- `src/product_workflow_runtime.py`: dependency doctor, fresh journal initialization, reused intake, real planning/review consultation adapter, and forwarding of canonical commands to the frozen controller. Planning receipts bind inputs; unresolved intents and invalid cached receipt digests fail closed. Human decision resolution stays in `workflow_answer` and is excluded from `workflow_run` commands.
- `src/workflow_mcp.py`: explicit configured V2 entry selection. Existing injected/default V1 paths remain available as V1.
- `src/runtime_composition.py`, `schemas/runtime_composition.v1.schema.json`: extend existing configuration with lifecycle version, explicit bridge profile and transport.
- `scripts/research_workflow_cli.py`: `init --workspace ... --runtime-config ...`, using the Product dependency doctor and existing controller initialization. No Stage or old `.research` needed.
- `.gitignore`: ignore generated `.workflow-v2/` runtime.
- `tests/test_product_bootstrap_cli.py`, `tests/test_product_workflow_runtime.py`: clean initialization, isolated legacy-path rejection, entry selection, immutable planning intent/receipt, validation and error boundaries.

The nonsecret machine configuration is outside the repository at `%LOCALAPPDATA%/ResearchWorkflow/product-v2-runtime.json`. It explicitly selects V2, `.workflow-v2/journal.json`, `openai-codex`, `gpt-5.6-luna`, ChatGPT auth, no fallback, and the installed bridge's configured locations/transport. Those actual installation paths are machine settings, not hard-coded product authority.

The machine `research-supervisor` MCP registration was updated to this Product launcher plus the configuration environment variable. This conversation's already-loaded MCP process still exposes the old HOST response shape. Real validation therefore launched the newly registered Product `scripts/workflow_mcp.py` STDIO server and called its actual `tools/call` → `workflow_run`, saving request/response evidence. The attached old process needs reconnection before direct attached tools reflect the new entry; it was not used for Stage mutation.

## Repeatable bootstrap path

1. Checkout product files and install their Python/Node/Codex/Bridge dependencies.
2. Configure the existing runtime-composition schema in a machine-local nonsecret file, explicitly selecting V2 and installed dependency paths; use normal Codex/ChatGPT authentication.
3. Run `python scripts/research_workflow_cli.py init --workspace <project> --runtime-config <machine-config>`.
4. Register/load the Product MCP launcher with the same config environment variable.
5. Use `workflow_start` / `workflow_answer` for project intake if needed, then `workflow_run` for real Stage planning; `workflow_resume` reads the existing V2 journal.

No old `.research`, `.tmp`, consultations, workflow-state, receipts, or project identity is required by initialization. The Product currently requires an explicit V2 configuration; it does not silently treat an unspecified legacy configuration as V2. Packaging and full new-machine installation remain subsequent portability work, not claims established by these bootstrap checks.

## Validation

| Required check | Result | Evidence / limit |
|---|---|---|
| A — clean candidate bootstrap | PASS | Fresh empty `clean-a` project, new journal and runner ready, no old `.research` |
| B — missing machine config | PASS | Explicit missing config → exit 2 / `RUNTIME_CONFIG_INVALID`, workspace unchanged; no old-runtime fallback |
| C — no old runtime dependency | PASS, bounded | Isolated temporary roots contain no old `.research`, `.tmp`, or consultations; test denies legacy runtime builders; initialization succeeds without `.research`. This is dependency isolation, not OS-wide denial of filesystem access by all child processes |
| D — relocated Product | PASS | Only product source/scripts/schemas copied to a different temporary root, no runtime; relocated CLI initializes a second empty project with the same machine config |
| E — lifecycle and entry regressions | PASS | 99 tests passed across frozen V2, runtime composition/MCP/CLI, new Product bootstrap/runtime, and launcher transport suites |
| Codex auth / model discovery | PASS | CLI 0.154.0, saved ChatGPT auth discovered, exact standard model available |
| Browser auth | PASS for actual operation | Real fresh planning consultation validated conversation identity and packet digest |
| Canonical `workflow_run` | PASS | Product MCP actual response: runner_ready=true, stage_created=true, WORKFLOW_DECISION=CONTINUE |

Relocation evidence: `.research/product-bootstrap/portability-validation.json`; isolated copy root `D:/Temp/workflow-product-portability-w7ef2bcj` (evidence path only).

Test command (global unrelated plugin autoload disabled because the first attempt failed before collection in machine Dash/Jupyter):

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q -p no:cacheprovider tests/test_workflow_v2_contracts.py tests/test_workflow_v2_controller.py tests/test_workflow_v2_invariants.py tests/test_workflow_v2_runtime.py tests/test_workflow_v2_migration.py tests/test_runtime_composition.py tests/test_workflow_runtime_mcp.py tests/test_research_workflow_cli.py tests/test_product_bootstrap_cli.py tests/test_product_workflow_runtime.py tests/test_mcp_launcher_transport.py
```

Actual result: `99 passed in 10.05s`.

## Real canonical planning and continuation

Stage: `spec-kit-adoption-and-repository-architecture-v1`.
Planning consultation: `CONSULT-20260912-122248-37b75606`.
Conversation: `6aa5443f-7564-83ee-9faa-3ed2d1448b8e`.
`request_count=1`, `conversation_validated=true`, `WORKFLOW_DECISION: CONTINUE`.
Packet SHA-256: `8b8efe0fd9add86cb442afce2b089d44432f75b50630d38d13ee1ae94b3908d5`.
Response SHA-256: `bf71b657322801320bd5389b371c40b2808f3583ddb958b858057dd088c8bbbc`.
Actual request/response: `.research/product-bootstrap/plan-request-v2.json`, `plan-response-v2.json`; sanitized Bridge receipt under `.consultations/<consultation-id>/receipt.json`.

An earlier explicit attempt failed in local Bridge timeout validation before sending a prompt (configured 600000 exceeded Bridge maximum 300000). Its intent and error were preserved. The adapter now caps timeout and returns bounded errors. The successful planning used an explicit new artifact revision, not an automatic retry or discarded failed evidence. See `.research/product-bootstrap/PREFLIGHT_FAILURE.md`.

Canonical START and REQUEST_EXECUTION subsequently succeeded. Research/design execution uses this Codex session and explicitly configured Luna/max collaboration agents; no new native-provider invocation is claimed. Final execution/assessment/GPT Technical Review and Human gate evidence belongs to this Stage. The bootstrap PASS does not mean architectural migration is accepted or executed.

Hard-coded machine paths introduced in tracked product defaults/code: **NO**.
Old runtime copied: **NO**. Lifecycle kernel changed: **NO**.
HOST/frozen candidates modified: **NO**. Mass move/delete/rename or Spec Kit migration: **NO**.
