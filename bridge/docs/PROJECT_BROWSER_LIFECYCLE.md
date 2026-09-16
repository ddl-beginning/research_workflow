# Project Browser lifecycle

This document describes the machine-local transport contract owned by
`ProjectBrowserManager`. It does not implement or prescribe consultation
orchestration, prompt retry, response retry, receipt decisions, or
authentication UX; those decisions remain with the main agent.

## Ownership and binding

- An explicit Workflow `project_id` owns one Chromium process and one
  persistent profile below the machine runtime root.
- The bound ChatGPT Project URL is retained and compared as a separate
  binding. It is never inferred from a working directory.
- The registry is rebuildable transport evidence. It contains the PID, CDP
  port, profile digest, runtime instance, process identity, bounded restart
  history, and current lease state.
- New launches carry a machine-local project id and binding marker in their
  Chromium arguments. The manager also scans the live OS process table for
  the configured `--user-data-dir`; registry entries alone are not proof that
  a profile is free.
- A live owner for another Project, or an owner whose binding cannot be
  established safely, blocks a new start with `PROFILE_ALREADY_IN_USE`.

## Process-loss recovery

The manager verifies a PID with a process-start identity (Windows process
creation time, `/proc` start ticks, or the bounded `ps` fallback). A PID by
itself is never enough to reconnect or terminate a process. The identity is
checked before and after CDP reconnect; a verified identity is checked again
before a failed browser is stopped. A reused PID is therefore recorded as
stale and is not killed.

One `acquire()` may perform at most one bounded restart
(`PROJECT_BROWSER_RESTART_LIMIT = 1`). A restart keeps the same project,
profile, and binding. Each recovered attempt appends a bounded history event:

- `PROJECT_BROWSER_PROCESS_LOST` means the recorded process was dead or its
  identity no longer matched.
- `RECOVERABLE_INFRASTRUCTURE_FAILURE` means a verified process could not be
  reached through its CDP transport/context and was safely stopped before the
  one restart.
- A successful replacement marks the event `recovered: true` and records only
  the old/new PIDs, classifications, reason code, and timestamps.

The handle evidence is sanitized: it exposes a process-identity digest and
start time, never a command line, credential material, or raw process-table
dump. No watcher or daemon is started; discovery and recovery happen only
during bounded manager calls.

## Lease reconciliation

The per-Project consultation lease is an exclusive directory plus metadata.
The old ten-minute age is retained only as compatibility telemetry; it is
not an expiry rule. If the owner PID is live, an identity match keeps the
lease busy regardless of age. If metadata has no identity, or the owner
identity cannot be checked, acquisition fails closed rather than taking the
lease. A lease is reconciled only when its owner is proven dead or the live
PID is proven to belong to a different process (PID reuse/machine restart).

## Compact failure matrix

| Observation boundary | Manager result | Classification / boundary |
| --- | --- | --- |
| Before prompt; browser PID dead or stale | Rebuild the same project/profile/binding once | `PROJECT_BROWSER_PROCESS_LOST` plus recovered `RECOVERABLE_INFRASTRUCTURE_FAILURE` evidence; prompt policy is outside this manager |
| After prompt submission; browser process disappears | Preserve the bounded process-loss evidence and return the restarted handle if recovery succeeds | The manager does not infer whether the prompt committed or choose a retry |
| During response extraction; verified CDP/context failure | Stop only the matching process, then perform the one restart | `RECOVERABLE_INFRASTRUCTURE_FAILURE`; response/receipt semantics are outside this manager |
| Machine/bridge restart; registry or lease is stale | Adopt a live owner with matching profile/binding, or reconcile only a proven-dead/PID-reused owner | No age-only lease steal; no duplicate profile start |
| Authentication/login or human verification | Keep transport bounded and surface the existing manual boundary | No profile copy, credential transfer, bypass, or automatic auth claim |
| Stale PID with a different live identity | Do not reconnect or kill that PID; inspect the actual profile owner and recover only if safe | `PROJECT_BROWSER_PROCESS_LOST` when a replacement is required |
| Live lease, even older than ten minutes | Reject acquisition as busy | Age alone never supersedes a live owner |
| Profile already owned by another/unknown live process | Reject before spawning | `PROFILE_ALREADY_IN_USE` |

Explicit shutdown is separate from consultation release and Stage lifecycle.
StageController remains the business lifecycle authority; registry status
cannot create or complete a Stage. The legacy unscoped bridge path remains
compatible but does not claim per-Project ownership because it has no explicit
Project identity.
