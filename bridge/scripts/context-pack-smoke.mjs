#!/usr/bin/env node

import path from 'node:path';
import process from 'node:process';
import crypto from 'node:crypto';
import {
  BRIDGE_ROOT,
  CONVERSATION_MODES,
  consultOnce,
  DEFAULT_PROFILE_DIR,
  FAILURE_CODES,
  resolveResponseTimeoutMs,
} from '../src/bridge.mjs';
import {
  buildContextPack,
  CONTEXT_PACK_MODES,
  createContextPacketId,
} from '../src/context-pack.mjs';
import { buildReviewerPrompt, writeLocalReviewSummary } from '../src/dialogue-policy.mjs';

const fixtureRoot = path.join(BRIDGE_ROOT, 'test', 'fixture_project');
const safeTextFixture = path.join(fixtureRoot, 'CURRENT_RESULT.md');
const profileDir = path.resolve(process.env.CHATGPT_PROFILE_DIR || DEFAULT_PROFILE_DIR);
const timeoutValue = process.env.CHATGPT_RESPONSE_TIMEOUT_MS;
let responseTimeoutMs;

const shared = {
  rootDir: BRIDGE_ROOT,
  evidenceRoots: [fixtureRoot],
  projectGoal: 'Estimate a shoreline boundary while preserving the visible boundary shape at review scale.',
  currentStageGoal: 'Determine whether fixed-radius cleanup removes the thin boundary.',
  userVisibleGoal: 'Produce a stable, reviewable boundary result.',
  establishedFacts: [
    'The after output has fewer speckle components.',
    'The upper-right boundary is visibly thinner.',
  ],
  currentMethod: 'Fixed-radius binary erosion precedes contour extraction.',
  currentBlocker: 'Cleanup improves speckle while lowering boundary overlap.',
  protectedForbiddenScope: [
    'Do not edit the real facade project.',
    'Do not scan or select evidence beyond the explicit fixture paths.',
  ],
  hardConstraints: ['At most nine packet attachments.', 'Exactly one prompt per invocation.'],
  previousRelevantDecisions: [
    { text: 'Compare direct observable outputs before changing the implementation.', kind: 'decision', source: 'codex' },
  ],
  latestResult: {
    actualWork: 'Ran the fixture cleanup and selected before/after outputs.',
    tested: ['Fixture metric comparison', 'Bounded bridge regression tests'],
    success: ['Speckle components fell from 37 to 8.'],
    failure: ['Boundary IoU fell from 0.84 to 0.79.'],
    change: 'The result is cleaner but loses the upper-right boundary segment.',
    whyConsult: 'Determine whether a representation or measurement route is preferable to tuning the radius.',
    localReferences: ['test/fixture_project/CURRENT_RESULT.md'],
  },
  metrics: {
    boundary_iou: { before: 0.84, after: 0.79 },
    boundary_f1: { before: 0.91, after: 0.86 },
    speckle_components: { before: 37, after: 8 },
  },
  diffSummary: 'src/example.py applies a fixed-radius erosion before contour extraction.',
  sourceContext: [{
    filePath: 'test/fixture_project/src/example.py',
    relevantFunctions: ['remove_low_confidence', 'extract_boundary'],
    excerpt: 'def remove_low_confidence(mask, radius=2):\n    return binary_erosion(mask, radius=radius)',
    whyRelevant: 'This is the selected operation that may erase the thin boundary.',
  }],
};

// A/B intentionally remove optional generated context so the first gate tests
// the builder's two required files plus one explicitly selected safe TXT. C
// uses the fuller realistic fixture after the lower-volume paths pass.
const minimalShared = { ...shared };
delete minimalShared.metrics;
delete minimalShared.diffSummary;
delete minimalShared.sourceContext;

function makeId() {
  return createContextPacketId(new Date(), crypto.randomUUID());
}

async function consultPacket(pack, question, mode, continueFrom = undefined) {
  const result = await consultOnce(buildReviewerPrompt({ question, mode: pack.mode, packetId: pack.packetId }), {
    rootDir: BRIDGE_ROOT,
    profileDir,
    mode,
    ...(continueFrom === undefined ? {} : { continueFrom }),
    contextPack: pack,
    ...(Number.isFinite(responseTimeoutMs) && responseTimeoutMs > 0 ? { responseTimeoutMs } : {}),
    log: (message) => console.log(message),
  });
  if (result.requestCount !== 1) throw new Error(`Expected request_count=1, got ${result.requestCount}`);
  await writeLocalReviewSummary({ consultationDir: result.consultationDir, responseText: result.responseText });
  console.log(JSON.stringify({
    packet_id: pack.packetId,
    packet_manifest: pack.manifestPath,
    packet_attachment_count: pack.attachmentCount,
    consultation_id: result.consultationId,
    conversation_id: result.conversationId,
    receipt: result.receiptPath,
    request_count: result.requestCount,
  }));
  return result;
}

try {
  responseTimeoutMs = timeoutValue === undefined ? undefined : resolveResponseTimeoutMs(timeoutValue);
  const packetA = await buildContextPack({
    ...minimalShared,
    packetId: makeId(),
    mode: CONTEXT_PACK_MODES.NORMAL,
    // The builder always emits STAGE_CONTEXT.md and LATEST_RESULT.md. Add one
    // explicit safe TXT to make this first gate exactly three attachments.
    evidence: [{ logicalName: 'smoke-safe.txt', sourcePath: safeTextFixture, role: 'result' }],
  });
  const resultA = await consultPacket(
    packetA,
    'Diagnose the trade-off. Keep NORMAL_ONLY_RECOMMENDATION_7f2c as a prior recommendation, not an established fact.',
    CONVERSATION_MODES.FRESH,
  );

  const packetB = await buildContextPack({
    ...minimalShared,
    packetId: makeId(),
    mode: CONTEXT_PACK_MODES.NORMAL,
    latestResult: {
      ...shared.latestResult,
      actualWork: 'Ran a second bounded comparison with the same stage context.',
      change: 'The second result preserves more of the thin boundary but leaves six speckle components.',
      whyConsult: 'Re-evaluate the route using the new output in the existing conversation.',
    },
    evidence: [
      { logicalName: 'RESULT_B.md', content: 'boundary_iou=0.86\nspeckle_components=6\n', role: 'result' },
      { logicalName: 'result_after.png', sourcePath: path.join(fixtureRoot, 'result_after.png'), role: 'review' },
    ],
    previousPack: packetA,
  });
  const resultB = await consultPacket(
    packetB,
    'Based on the new result, re-evaluate the diagnosis and state whether the route should continue or replan.',
    CONVERSATION_MODES.CONTINUE,
    resultA.consultationId,
  );

  const packetC = await buildContextPack({
    ...shared,
    packetId: makeId(),
    mode: CONTEXT_PACK_MODES.FRESH,
    previousRelevantDecisions: [],
    evidence: [
      { logicalName: 'result_before.png', sourcePath: path.join(fixtureRoot, 'result_before.png'), role: 'review' },
      { logicalName: 'result_after.png', sourcePath: path.join(fixtureRoot, 'result_after.png'), role: 'review' },
    ],
  });
  const resultC = await consultPacket(
    packetC,
    'Review this result from first principles. Do not assume any prior recommendation or hidden conversation context.',
    CONVERSATION_MODES.FRESH,
  );
  const freshIsolationVerified = !resultC.responseText.includes('NORMAL_ONLY_RECOMMENDATION_7f2c');
  if (!freshIsolationVerified) throw new Error('Fresh response unexpectedly contained the NORMAL-only marker.');
  console.log(JSON.stringify({
    status: 'complete',
    normal_continuation_verified: resultA.conversationId === resultB.conversationId,
    fresh_isolation_verified: freshIsolationVerified,
    fresh_conversation_isolated: resultC.conversationId !== resultA.conversationId,
    request_counts: [resultA.requestCount, resultB.requestCount, resultC.requestCount],
    receipts: [resultA.receiptPath, resultB.receiptPath, resultC.receiptPath],
  }));
} catch (error) {
  const code = error?.code || FAILURE_CODES.UNEXPECTED_PAGE_STATE;
  console.error(`CONTEXT_PACK_SMOKE_NOT_READY ${code}`);
  if (error?.message) console.error(`${code} ${error.message}`);
  process.exitCode = 1;
}
