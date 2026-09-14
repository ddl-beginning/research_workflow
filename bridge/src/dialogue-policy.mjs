import fs from 'node:fs/promises';
import path from 'node:path';

export const DIALOGUE_POLICY_SCHEMA_VERSION = 'dialogue_policy.v1';

// This is deliberately a small, closed vocabulary.  The policy layer only
// parses this routing decision; it never interprets a recommendation as an
// instruction and never invokes the bridge.
export const DIALOGUE_DECISIONS = Object.freeze([
  'CONTINUE',
  'REPLAN',
  'STAGE_READY',
  'HUMAN_GATE',
  'BLOCKED',
]);

export const DIALOGUE_DECISION_MARKER = 'WORKFLOW_DECISION';
export const DIALOGUE_DECISION_SCHEMA_VERSION = 'dialogue_decision.v1';

const DIALOGUE_DECISION_SET = new Set(DIALOGUE_DECISIONS);
const DIALOGUE_DECISION_TOKEN_RE = new RegExp(
  `^\\s*${DIALOGUE_DECISION_MARKER}\\s*:\\s*(\\S+)\\s*$`,
);
const DIALOGUE_DECISION_LABEL_RE = new RegExp(`\\b${DIALOGUE_DECISION_MARKER}\\b`, 'i');

export const REVIEWER_PROMPT_TEMPLATE = `You are the technical/research reviewer for the current Stage.

Read the attached context as evidence.

First understand the real Stage/output goal.

Analyze the latest result.

Do not assume the current implementation is necessary. If the current problem is representation-induced, consider whether that representation can be removed. Consider simpler alternatives, real-world physics, scale separation, and direct measurement evidence when relevant.

Then give:
1. current diagnosis;
2. recommended route;
3. concrete next Codex action;
4. what result should cause this route to stop/replan;
5. whether the current result is already suitable for Stage review.

Do not optimize merely because further optimization is possible.

Do not claim that an unobserved fact is established. Keep recommendations separate from observations and say when evidence is insufficient.

Finally, emit exactly one standalone machine-readable routing line, using one
of the five values below and no other value:
WORKFLOW_DECISION: CONTINUE
WORKFLOW_DECISION: REPLAN
WORKFLOW_DECISION: STAGE_READY
WORKFLOW_DECISION: HUMAN_GATE
WORKFLOW_DECISION: BLOCKED

This marker is only a bounded workflow decision for the caller. It does not
authorize bridge calls, code changes, execution of recommendations, or a
second consultation. STAGE_READY is not user approval.
`;

export const RESULT_REVIEW_OUTPUT_FIELDS = Object.freeze([
  'diagnosis',
  'recommended_route',
  'next_action',
  'stop_or_replan_condition',
  'stage_review_recommended',
]);

function normalizeQuestion(question) {
  if (typeof question !== 'string' || !question.trim()) throw new TypeError('question must be a non-empty string');
  return question.trim();
}

export function buildReviewerPrompt({ question, mode = 'normal', packetId = undefined } = {}) {
  const normalizedQuestion = normalizeQuestion(question);
  const normalizedMode = String(mode).toLowerCase();
  if (!['normal', 'fresh'].includes(normalizedMode)) throw new TypeError('mode must be normal or fresh');
  const packetLine = packetId ? `Context packet: ${packetId}\n` : '';
  const freshLine = normalizedMode === 'fresh'
    ? 'This is an independent architecture review. Do not rely on prior GPT recommendations unless they are present as observable evidence in this packet.\n'
    : 'This is a normal Stage consultation. Use prior relevant decisions only as context, and distinguish them from current observations.\n';
  return `${REVIEWER_PROMPT_TEMPLATE}\n${freshLine}${packetLine}\nReviewer question:\n${normalizedQuestion}\n`;
}

/**
 * Parse the one explicit workflow-routing marker from a GPT response.
 *
 * The marker must occupy a complete line and must occur exactly once.  A
 * missing, unknown, duplicated, or malformed marker returns null so callers
 * cannot accidentally turn an ambiguous response into an executable policy
 * decision.  The original response is intentionally not changed or embedded
 * in this result; consultOnce keeps the transport response in memory and
 * persists only bounded receipt metadata.
 */
export function parseDialogueDecision(responseText) {
  if (typeof responseText !== 'string' || !responseText.trim()) return null;

  const lines = responseText.split(/\r?\n/);
  const markerLines = lines.filter((line) => DIALOGUE_DECISION_LABEL_RE.test(line));
  if (markerLines.length !== 1) return null;

  const match = markerLines[0].match(DIALOGUE_DECISION_TOKEN_RE);
  if (!match || !DIALOGUE_DECISION_SET.has(match[1])) return null;

  return Object.freeze({
    schema_version: DIALOGUE_DECISION_SCHEMA_VERSION,
    decision: match[1],
  });
}

function stripCodeFence(value) {
  const trimmed = value.trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  return fenced ? fenced[1].trim() : trimmed;
}

export function parseLocalReviewSummary(responseText) {
  if (typeof responseText !== 'string' || !responseText.trim()) return null;
  const candidates = [stripCodeFence(responseText)];
  const objectStart = responseText.indexOf('{');
  const objectEnd = responseText.lastIndexOf('}');
  if (objectStart >= 0 && objectEnd > objectStart) candidates.push(responseText.slice(objectStart, objectEnd + 1));
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) continue;
      const summary = {};
      for (const field of RESULT_REVIEW_OUTPUT_FIELDS) {
        if (field === 'stage_review_recommended') {
          if (typeof parsed[field] !== 'boolean') continue;
          summary[field] = parsed[field];
        } else if (typeof parsed[field] === 'string' && parsed[field].trim()) {
          summary[field] = parsed[field].trim();
        } else {
          continue;
        }
      }
      if (RESULT_REVIEW_OUTPUT_FIELDS.every((field) => Object.hasOwn(summary, field))) return summary;
    } catch {
      // A free-form GPT response is valid. Summary parsing is deliberately
      // best-effort and never changes the transport result.
    }
  }
  return null;
}

export async function writeLocalReviewSummary({ consultationDir, responseText } = {}) {
  if (typeof consultationDir !== 'string' || !consultationDir.trim()) throw new TypeError('consultationDir is required');
  const summary = parseLocalReviewSummary(responseText);
  if (!summary) return null;
  const summaryPath = path.join(consultationDir, 'review_summary.json');
  await fs.writeFile(summaryPath, `${JSON.stringify({
    schema_version: DIALOGUE_POLICY_SCHEMA_VERSION,
    ...summary,
  }, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  return summaryPath;
}
