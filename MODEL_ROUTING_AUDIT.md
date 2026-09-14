# Workflow V2.1 Model Routing Audit

Stage: `workflow-v2.1-agent-routing-and-human-output-v1`

Audited locally on 2026-09-14 with Codex CLI `0.154.0`, `codex login status`,
`codex debug models`, and `codex exec --help`.  The installed catalog exposed
`gpt-5.6-luna` and `gpt-6-astra`; both route bindings are also covered by the
offline executor seam tests.

| Surface | Finding |
| --- | --- |
| Parent Codex session model | Owned by the host/session. Workflow does not inspect or change the Desktop model picker. |
| Child agent mechanism | The current supported product seam is one isolated `codex exec --json --ephemeral` subprocess per execution request. No native Codex CLI child-agent API with a per-child model override was exposed by this installed CLI surface. |
| `codex exec` mechanism | `OpenAICodexExecutor` builds the command with `--model`, `--cd`, `--sandbox workspace-write`, `--skip-git-repo-check`, and JSONL output. |
| Model override | Real CLI argument: `--model <resolved catalog id>`. The requested route is resolved against `codex debug models`; unavailable models fail closed. |
| Reasoning override | Real CLI config override: `-c model_reasoning_effort="<effort>"`. |
| Auth inheritance | Saved ChatGPT Codex login is used through the normal Codex home/session. Ambient API-key, token, password, and secret environment variables are removed before the child process starts. |
| Sandbox inheritance | The child receives the explicit `workspace-write` sandbox. Windows only receives the bounded `windows.sandbox="unelevated"` compatibility override needed to spawn the process; it does not bypass the Codex execution sandbox. |
| Result/receipt binding | Metadata-only `request.json`, `events.jsonl`, `run.json`, and `workspace.diff` are written outside the workspace when configured. Each run records requested route/model/effort, selected actual model/effort, parent/child operation IDs, and a result identity. Raw prompts, raw JSONL, credentials, cookies, and full transcripts are not persisted. |
| Failure behavior | Missing auth, missing catalog entries, model capacity, spawn failures, and incomplete turns become bounded provider failures. `FRONTIER` has no implicit fallback to `STANDARD`; only the existing explicitly configured `STANDARD` compatibility fallback may select a different actual model, and that selection remains visible in the receipt. |

## Canonical routing

| Route | Model | Reasoning | Authority |
| --- | --- | --- | --- |
| `STANDARD` | `gpt-5.6-luna` | `max` | `src/executor_model_routing.py` |
| `FRONTIER` | `gpt-6-astra` | `low` | `src/executor_model_routing.py` |

`FRONTIER` requires an authoritative executor request. Escalation remains
bounded by the existing two-failure plus authoritative same-objective review
policy. Ordinary test, path, syntax, and transport failures do not promote a
request by heuristic.

## Parent-model independence

The parent model is not a routing input. The executor request binds its own
route and passes the selected child model to `codex exec`. Offline seam tests
cover Luna-parent/FRONTIER-child and Astra-parent/STANDARD-child metadata
scenarios; the second scenario is a host/process-seam check, not a Desktop UI
claim. A live provider run is intentionally not part of the repository test
suite because it would mutate a workspace and consume an external model turn.
