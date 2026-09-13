# Workflow V2 Product Release Notes

## What V2 provides

- One durable, content-addressed V2 lifecycle journal and executable-owner
  token.
- Project-scoped briefs and artifacts with explicit engine/project/history/TEMP
  ownership.
- Real Codex Provider execution bound to `STANDARD → gpt-5.6-luna → max →
  chatgpt`.
- Real GPT planning and Technical Review consultations with packet and
  conversation provenance.
- Crash-safe resume, explicit external-effect settlement, technical
  `STAGE_READY`, integration, verification, and canonical closeout.
- Clean-checkout installation documentation, a read-only doctor, and an
  explicit MCP registration contract.

## Main difference from V1

V1's legacy bootstrap/orchestrator path is not the Product entry for V2.
Explicit `lifecycle_version: "v2"` selects the Product adapter and the frozen
V2 `StageController`; the old host and frozen V2 checkout remain read-only.
V2 also separates durable project artifacts from engine files and
machine-local temporary runtime data.

## Known limitations

- Browser Bridge is an external, headed dependency and requires normal manual
  ChatGPT authentication on a new machine.
- GPT consultation is one-shot per invocation; failed or unresolved effects
  are not automatically retried.
- The Product does not install Codex CLI or Browser Bridge for the operator.
- Historical missing-receipt fixture failures remain documented separately;
  no synthetic receipts are created.
- The retained cleanup/quarantine/deferred items from the prior cleanup stage
  are known retained state and are outside this release stage.

## Migration expectations

Use a new project checkout and create a new V2 machine config. Do not migrate
old `.research`, `.workflow-v2`, consultation, or browser-profile state into a
new project unless a future explicitly authorized migration stage defines that
operation. Existing V1 projects remain on their existing host and are not
silently converted.

## Validation

The release candidate `workflow-v2-product-rc7` passed the clean-room
installation, real Provider/GPT lifecycle, resume, relocation, and
documentation audit. See [RELEASE_VALIDATION.md](RELEASE_VALIDATION.md) for
the bounded evidence and the two pre-existing historical fixture failures.
