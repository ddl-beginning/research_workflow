# Workflow V2 Product Architecture Constitution

Version: `1`

## Governance

Human decisions govern project intent, architecture, and any destructive
operation. Accepted decisions retain their rationale and are never silently
rewritten.

## Authority principles

The V2 `StageController` journal is the only lifecycle authority. Provider
observations, assessments, decisions, integration receipts, and verification
receipts are immutable or versioned records owned by their canonical paths.
Human guidance explains intent and work; it cannot change machine state.

## Project-wide constraints

Keep Workflow Engine, Project Workspace, and Machine-local Runtime ownership
separate. Preserve valuable Flow-forward history. Keep credentials and machine
paths outside project authority. Treat unknown ownership as retained until a
reviewed decision resolves it.

## Change rules

Guidance changes require a version and an accepted intent digest. A plan or
task may narrow work but may not expand the Stage contract. New requirements
use a successor Stage. Cleanup requires a fresh manifest and a separate Human
destructive-action gate.
