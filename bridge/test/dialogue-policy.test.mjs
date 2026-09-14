import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  writeConsultationArtifacts,
} from '../src/bridge.mjs';
import {
  DIALOGUE_DECISIONS,
  DIALOGUE_DECISION_SCHEMA_VERSION,
  buildReviewerPrompt,
  parseDialogueDecision,
  writeLocalReviewSummary,
} from '../src/dialogue-policy.mjs';

const BRIDGE_ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));

async function makeTestRoot() {
  return fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-policy-'));
}

async function removeTestRoot(rootDir) {
  await fs.rm(rootDir, { recursive: true, force: true });
}

test('dialogue policy parses each of the five closed workflow decisions', () => {
  assert.deepEqual(new Set(DIALOGUE_DECISIONS), new Set([
    'CONTINUE',
    'REPLAN',
    'STAGE_READY',
    'HUMAN_GATE',
    'BLOCKED',
  ]));
  assert.equal(DIALOGUE_DECISIONS.length, 5);

  for (const decision of DIALOGUE_DECISIONS) {
    const parsed = parseDialogueDecision(
      `Diagnosis and recommendation remain natural language.\nWORKFLOW_DECISION: ${decision}\n`,
    );
    assert.deepEqual(parsed, {
      schema_version: DIALOGUE_DECISION_SCHEMA_VERSION,
      decision,
    });
  }
});

test('dialogue decision parser fails closed for missing, unknown, conflicting, or malformed markers', () => {
  const invalidResponses = [
    undefined,
    null,
    '',
    '   \n',
    42,
    'CONTINUE',
    'The route should continue, but no machine marker was supplied.',
    'WORKFLOW_DECISION:',
    'WORKFLOW_DECISION: MAYBE',
    'WORKFLOW_DECISION: continue',
    'WORKFLOW_DECISION: CONTINUE extra',
    '- WORKFLOW_DECISION: CONTINUE',
    '"WORKFLOW_DECISION":"CONTINUE"',
    'WORKFLOW_DECISION: CONTINUE\nWORKFLOW_DECISION: REPLAN',
    'WORKFLOW_DECISION: CONTINUE\nWORKFLOW_DECISION: CONTINUE',
    'WORKFLOW_DECISION: CONTINUE\nThe phrase WORKFLOW_DECISION is quoted here.',
  ];

  for (const response of invalidResponses) {
    assert.equal(parseDialogueDecision(response), null, `expected fail closed: ${String(response)}`);
  }
});

test('reviewer prompt requests one workflow marker without turning it into an execution trigger', () => {
  const prompt = buildReviewerPrompt({
    question: 'Should the current representation continue?',
    mode: 'normal',
    packetId: 'PACK-policy-test-0001',
  });
  assert.match(prompt, /WORKFLOW_DECISION: CONTINUE/);
  assert.match(prompt, /exactly one standalone/i);
  assert.match(prompt, /authorize bridge calls/i);
  assert.match(prompt, /STAGE_READY is not user approval/i);
});

test('policy parsing and local summary writing keep raw response transport-only', async () => {
  const rootDir = await makeTestRoot();
  const prompt = 'PROMPT_SENTINEL must never be written to the consultation directory.';
  const rawResponse = [
    'Observed: the measured boundary is stable at the target scale.',
    JSON.stringify({
      diagnosis: 'The selected cleanup removes a thin boundary.',
      recommended_route: 'Compare a direct scale-aware measurement.',
      next_action: 'Run one bounded comparison on the fixture.',
      stop_or_replan_condition: 'Replan if the direct measurement is unstable.',
      stage_review_recommended: false,
    }),
    'WORKFLOW_DECISION: REPLAN',
    'RAW_RESPONSE_SENTINEL must remain transport-only.',
  ].join('\n');
  try {
    const artifacts = await writeConsultationArtifacts({
      rootDir,
      consultationId: 'CONSULT-20260904-060000-aabbccdd',
      createdAt: '2026-09-04T06:00:00.000Z',
      prompt,
      profile: '.auth/chatgpt-profile',
      mode: 'fresh',
      conversationId: '12345678-1234-4234-8234-123456789abc',
      conversationRootConsultationId: 'CONSULT-20260904-060000-aabbccdd',
      conversationValidated: true,
      chatUrl: 'https://chatgpt.com/c/12345678-1234-4234-8234-123456789abc',
      status: 'complete',
      responseText: rawResponse,
      requestCount: 1,
    });

    assert.deepEqual(parseDialogueDecision(rawResponse), {
      schema_version: DIALOGUE_DECISION_SCHEMA_VERSION,
      decision: 'REPLAN',
    });
    const summaryPath = await writeLocalReviewSummary({
      consultationDir: artifacts.consultationDir,
      responseText: rawResponse,
    });
    assert.ok(summaryPath);
    const consultationFiles = (await fs.readdir(artifacts.consultationDir)).sort();
    assert.deepEqual(consultationFiles, ['receipt.json', 'review_summary.json']);
    await assert.rejects(fs.access(artifacts.requestPath));
    await assert.rejects(fs.access(artifacts.responsePath));
    const receiptText = await fs.readFile(artifacts.receiptPath, 'utf8');
    const summaryText = await fs.readFile(summaryPath, 'utf8');
    assert.equal(JSON.parse(receiptText).response_char_count, rawResponse.length);
    assert.doesNotMatch(receiptText, /PROMPT_SENTINEL|RAW_RESPONSE_SENTINEL/);
    assert.doesNotMatch(summaryText, /PROMPT_SENTINEL|RAW_RESPONSE_SENTINEL/);
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('policy parser is pure and does not invoke a bridge or schedule another consultation', () => {
  let consultationCalls = 0;
  const fakeConsult = () => {
    consultationCalls += 1;
  };
  const parsed = parseDialogueDecision(
    'A bounded local result is available.\nWORKFLOW_DECISION: CONTINUE\n',
    fakeConsult,
  );
  assert.equal(parsed.decision, 'CONTINUE');
  assert.equal(consultationCalls, 0);
});
