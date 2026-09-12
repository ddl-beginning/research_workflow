# Phase 5 Architecture Gate

## V2 route counts

| Gate | Result |
| --- | ---: |
| Domain schema roots | 7 |
| Shared support schema roots | 3 |
| Writable Stage states | 5 |
| Canonical public commands | 16 |
| Shared guard families | 9 |
| V2 lifecycle journal writers | 1 |
| V2 assessment binders | 1 |
| V2 Human Decision resolvers | 1 |
| Executable owner tokens | 1 |
| V2 special recovery lifecycles | 0 |
| V2 bootstrap lifecycles | 0 |
| Independently writable V2 projections | 0 |

The counts are taken from the V2 contract constants and Phase 0 inventories.
The V2 route is `StageController.dispatch` plus its reducer handlers;
`WorkflowRuntimeV2` and `WorkflowV2ReceiptAdapter` only project or submit
commands. `BLOCKED` appears only as an operation status / derived waiting
commands. The controller uses one journal process lock and reload-before-CAS;
`BLOCKED` appears only as an operation status / derived waiting condition,
never as a writable Stage state.

## Regression gate

```text
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q
477 passed, 2 failed in 64.60s
```

The two failures are the known historical `workflow_self_test.py` fixture
failures caused by the intentionally absent
`.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`. No new V2
failure was introduced. Raw output is retained in
`implementation_evidence/phase-5/full-isolated-pytest-final.stdout.txt`.

## Decision

PASS for the isolated V2 architecture gate. The legacy V1 compatibility
surface remains in the candidate only to preserve the verified HOST regression
and history readers; it is not part of the V2 route and has no access to the
V2 journal. No architecture deviation was required.
