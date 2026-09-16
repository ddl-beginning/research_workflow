import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import {
  BRIDGE_ROOT,
  CHATGPT_URL,
  BridgeError,
  CONVERSATION_MODES,
  FAILURE_CODES,
  PROJECT_NAVIGATION_FAILURE_CLASSES,
  buildReceipt,
  readConsultationReceipt,
  writeConsultationArtifacts,
} from '../src/bridge.mjs';
import {
  computeRecoveryPromptHash,
  consultWithRecovery,
  consultationIntentPath,
  readConsultationIntent,
} from '../src/consultation-recovery.mjs';

const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';
const CHAT_URL = `https://chatgpt.com/c/${CONVERSATION_ID}`;
const PACK_HASH = 'a'.repeat(64);
const PROJECT_URL = 'https://chatgpt.com/g/g-p-outdoor-project/project';

function intentKey(value) {
  return crypto.createHash('sha256').update(value, 'utf8').digest('hex');
}

function recoveryOptions(rootDir, key, consult) {
  return {
    rootDir,
    mode: CONVERSATION_MODES.FRESH,
    recoveryPromptHash: computeRecoveryPromptHash({
      question: 'stable question',
      packSha256: PACK_HASH,
    }),
    consult,
  };
}

async function temporaryRoot() {
  return fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-consultation-recovery-'));
}

async function removeRoot(rootDir) {
  await fs.rm(rootDir, { recursive: true, force: true });
}

function sentResult(consultationId) {
  return {
    consultationId,
    requestCount: 1,
    responseText: 'initial response',
    conversationId: CONVERSATION_ID,
    chatUrl: CHAT_URL,
    conversationValidated: true,
  };
}

test('stable recovery prompt hash excludes packet id and generated time', () => {
  const first = computeRecoveryPromptHash({ question: ' stable question ', packSha256: PACK_HASH });
  const second = computeRecoveryPromptHash({ question: 'stable question', packSha256: PACK_HASH });
  assert.equal(first, second);
});

test('pre-prompt restart keeps one consultation id and sends exactly once', async () => {
  const rootDir = await temporaryRoot();
  const key = intentKey('pre-prompt-restart');
  let attempts = 0;
  let sendCount = 0;
  try {
    await assert.rejects(
      consultWithRecovery(
        'review prompt',
        recoveryOptions(rootDir, key, async () => {
          attempts += 1;
          throw Object.assign(
            new BridgeError(FAILURE_CODES.PROMPT_INPUT_NOT_FOUND, 'composer unavailable'),
            { requestCount: 0 },
          );
        }),
        { intentKey: key },
      ),
      (error) => error.code === FAILURE_CODES.PROMPT_INPUT_NOT_FOUND,
    );

    const result = await consultWithRecovery(
      'review prompt',
      recoveryOptions(rootDir, key, async (prompt, options) => {
        attempts += 1;
        await options.durability.beforePromptSend({ requestCount: 1 });
        sendCount += 1;
        await options.durability.conversationRoute({
          requestCount: 1,
          chatUrl: CHAT_URL,
          conversationId: CONVERSATION_ID,
        });
        return sentResult(options.consultationId);
      }),
      { intentKey: key },
    );

    assert.equal(attempts, 2);
    assert.equal(sendCount, 1);
    assert.equal(result.requestCount, 1);
    const intent = await readConsultationIntent({ rootDir, intentKey: key });
    assert.equal(intent.request_count, 1);
    assert.equal(intent.conversation_id, CONVERSATION_ID);
    assert.equal(intent.chat_url, CHAT_URL);
    assert.equal(JSON.stringify(intent).includes('review prompt'), false);
  } finally {
    await removeRoot(rootDir);
  }
});

test('post-prompt restart recovers the same conversation without sending again', async () => {
  const rootDir = await temporaryRoot();
  const key = intentKey('post-prompt-restart');
  let normalSendCount = 0;
  let recoveryCalls = 0;
  try {
    await assert.rejects(
      consultWithRecovery(
        'review prompt',
        recoveryOptions(rootDir, key, async (prompt, options) => {
          await options.durability.beforePromptSend({ requestCount: 1 });
          normalSendCount += 1;
          await options.durability.conversationRoute({
            requestCount: 1,
            chatUrl: CHAT_URL,
            conversationId: CONVERSATION_ID,
          });
          throw Object.assign(new BridgeError(FAILURE_CODES.RESPONSE_TIMEOUT, 'browser closed'), { requestCount: 1 });
        }),
        { intentKey: key },
      ),
      (error) => error.code === FAILURE_CODES.RESPONSE_TIMEOUT,
    );

    const result = await consultWithRecovery(
      'review prompt',
      recoveryOptions(rootDir, key, async (prompt, options) => {
        assert.equal(typeof options.recovery, 'object');
        assert.equal(options.recovery.conversationId, CONVERSATION_ID);
        assert.equal(typeof options.durability.beforePromptSend, 'function');
        recoveryCalls += 1;
        return {
          consultationId: options.consultationId,
          requestCount: 1,
          responseText: 'recovered analysis\nWORKFLOW_DECISION: CONTINUE',
          conversationId: options.recovery.conversationId,
          chatUrl: options.recovery.chatUrl,
          conversationValidated: true,
        };
      }),
      { intentKey: key },
    );

    assert.equal(normalSendCount, 1);
    assert.equal(recoveryCalls, 1);
    assert.equal(result.requestCount, 1);
    assert.equal(result.workflowDecision.decision, 'CONTINUE');
    const intent = await readConsultationIntent({ rootDir, intentKey: key });
    assert.equal(intent.status, 'complete');
    assert.equal(intent.chat_url, CHAT_URL);
  } finally {
    await removeRoot(rootDir);
  }
});

test('post-prompt recovery promotes target metadata without resending', async () => {
  const rootDir = await temporaryRoot();
  const key = intentKey('post-prompt-target-promotion');
  const stableHash = computeRecoveryPromptHash({ question: 'stable question', packSha256: PACK_HASH });
  let recoveryCalls = 0;
  try {
    await consultWithRecovery(
      'review prompt',
      {
        ...recoveryOptions(rootDir, key, async (prompt, options) => {
          await options.durability.beforePromptSend({ requestCount: 1 });
          await options.durability.conversationRoute({
            requestCount: 1,
            chatUrl: CHAT_URL,
            conversationId: CONVERSATION_ID,
          });
          return sentResult(options.consultationId);
        }),
        projectId: 'project-target-promotion',
        projectUrl: PROJECT_URL,
      },
      { intentKey: key },
    );

    const intent = await readConsultationIntent({ rootDir, intentKey: key });
    const receipt = buildReceipt({
      consultationId: intent.consultation_id,
      createdAt: '2026-09-16T12:00:00.000Z',
      profile: '.auth/chatgpt-profile',
      mode: CONVERSATION_MODES.FRESH,
      chatUrl: CHAT_URL,
      conversationId: CONVERSATION_ID,
      conversationValidated: true,
      status: 'complete',
      responseCharCount: 64,
      requestCount: 1,
      projectUrl: PROJECT_URL,
      projectScopeRequested: true,
      projectScopeVerified: true,
      projectScopeEvidence: {
        initial_navigation: {
          requested_url: PROJECT_URL,
          landed_url: PROJECT_URL,
          matched: true,
          verified: true,
        },
      },
      promptSha256: stableHash,
      includeTargetMetadata: true,
    });
    const receiptDir = path.join(rootDir, '.consultations', intent.consultation_id);
    await fs.mkdir(receiptDir, { recursive: true });
    await fs.writeFile(path.join(receiptDir, 'receipt.json'), `${JSON.stringify(receipt)}\n`, 'utf8');

    const result = await consultWithRecovery(
      'reconstructed reviewer prompt after restart',
      {
        ...recoveryOptions(rootDir, key, async (prompt, options) => {
          recoveryCalls += 1;
          assert.equal(options.recovery.conversationId, CONVERSATION_ID);
          return {
            consultationId: options.consultationId,
            requestCount: 1,
            responseText: 'recovered analysis\nWORKFLOW_DECISION: CONTINUE',
            conversationId: CONVERSATION_ID,
            chatUrl: CHAT_URL,
            conversationValidated: true,
          };
        }),
        projectId: 'project-target-promotion',
        projectUrl: PROJECT_URL,
        recoveryPromptHash: undefined,
      },
      { intentKey: key },
    );

    assert.equal(recoveryCalls, 1);
    assert.equal(result.requestCount, 1);
    assert.equal(result.workflowDecision.decision, 'CONTINUE');
    const promoted = await readConsultationIntent({ rootDir, intentKey: key });
    assert.equal(promoted.target_metadata.chatgpt_target_mode, 'PROJECT');
    assert.equal(promoted.target_metadata.chatgpt_project_target_verified, 'YES');
  } finally {
    await removeRoot(rootDir);
  }
});

test('recovery receipt merge preserves complete attachment metadata', async () => {
  const rootDir = await temporaryRoot();
  const consultationId = 'CONSULT-20260916-120000-aabbccdd';
  const readyAttachments = [{
    basename: 'LATEST_RESULT.md',
    relative_path: 'PACK-20260916-120000-aabbccdd/LATEST_RESULT.md',
    byte_size: 12,
    sha256: 'a'.repeat(64),
    media_type: 'text/markdown',
    upload_status: 'ready',
  }];
  const pendingAttachments = [{
    basename: 'LATEST_RESULT.md',
    relative_path: null,
    byte_size: null,
    sha256: null,
    media_type: 'text/markdown',
    upload_status: 'pending',
  }];
  try {
    const first = await writeConsultationArtifacts({
      rootDir,
      consultationId,
      createdAt: '2026-09-16T12:00:00.000Z',
      profile: '.auth/chatgpt-profile',
      status: 'complete',
      responseText: 'initial response',
      requestCount: 1,
      attachments: readyAttachments,
    });
    await writeConsultationArtifacts({
      rootDir,
      consultationId,
      createdAt: '2026-09-16T12:01:00.000Z',
      profile: '.auth/chatgpt-profile',
      status: 'recovery_pending',
      responseText: '',
      requestCount: 1,
      attachments: pendingAttachments,
      preserveExistingReceipt: first.receipt,
    });
    const loaded = await readConsultationReceipt({ rootDir, consultationId });
    assert.deepEqual(loaded.receipt.attachments, readyAttachments);
  } finally {
    await removeRoot(rootDir);
  }
});

test('interrupted write-ahead preserves count one and the durable route', async () => {
  const rootDir = await temporaryRoot();
  const key = intentKey('interrupted-write-ahead');
  try {
    await assert.rejects(
      consultWithRecovery(
        'review prompt',
        recoveryOptions(rootDir, key, async (prompt, options) => {
          await options.durability.beforePromptSend({ requestCount: 1 });
          await options.durability.conversationRoute({
            requestCount: 1,
            chatUrl: CHAT_URL,
            conversationId: CONVERSATION_ID,
          });
          throw new Error('process interrupted after write-ahead');
        }),
        { intentKey: key },
      ),
      (error) => error.requestCount === 1,
    );
    const intent = await readConsultationIntent({ rootDir, intentKey: key });
    assert.equal(intent.request_count, 1);
    assert.equal(intent.chat_url, CHAT_URL);
    assert.equal(intent.conversation_id, CONVERSATION_ID);
  } finally {
    await removeRoot(rootDir);
  }
});

test('explicit historical challenge adoption retains receipt target and auth class', async () => {
  const rootDir = await temporaryRoot();
  const consultationId = 'CONSULT-20260916-120000-aabbccdd';
  const key = intentKey('historical-challenge');
  const receipt = buildReceipt({
    consultationId,
    createdAt: '2026-09-16T12:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    chatUrl: CHATGPT_URL,
    status: FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
    projectNavigationDiagnostics: {
      failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE,
      http_status: 403,
      title: 'verification challenge',
    },
    requestCount: 0,
    includeTargetMetadata: true,
  });
  try {
    const receiptDir = path.join(rootDir, '.consultations', consultationId);
    await fs.mkdir(receiptDir, { recursive: true });
    await fs.writeFile(path.join(receiptDir, 'receipt.json'), `${JSON.stringify(receipt)}\n`, 'utf8');

    await assert.rejects(
      consultWithRecovery(
        'review prompt',
        recoveryOptions(rootDir, key, async () => {
          throw Object.assign(new BridgeError(FAILURE_CODES.PROJECT_NAVIGATION_FAILED, 'challenge'), { requestCount: 0 });
        }),
        { intentKey: key, recoverConsultationId: consultationId },
      ),
      (error) => error.code === FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
    );

    const retained = JSON.parse(await fs.readFile(path.join(receiptDir, 'receipt.json'), 'utf8'));
    assert.equal(retained.browser_failure_class, 'BROWSER_HUMAN_VERIFICATION_REQUIRED');
    assert.equal(retained.chatgpt_target_mode, 'DEFAULT');
    assert.equal(retained.chatgpt_target_origin, 'https://chatgpt.com');
    assert.equal((await fs.stat(consultationIntentPath({ rootDir, intentKey: key }))).isFile(), true);
  } finally {
    await removeRoot(rootDir);
  }
});

test('same intent is guarded against concurrent consultation runners', async () => {
  const rootDir = await temporaryRoot();
  const key = intentKey('same-intent-concurrency');
  let started;
  const startedPromise = new Promise((resolve) => { started = resolve; });
  let release;
  const releasePromise = new Promise((resolve) => { release = resolve; });
  let runnerCalls = 0;
  const consult = async (prompt, options) => {
    runnerCalls += 1;
    started();
    await releasePromise;
    await options.durability.beforePromptSend({ requestCount: 1 });
    return sentResult(options.consultationId);
  };
  try {
    const first = consultWithRecovery(
      'review prompt',
      recoveryOptions(rootDir, key, consult),
      { intentKey: key },
    );
    await startedPromise;
    await assert.rejects(
      consultWithRecovery(
        'review prompt',
        recoveryOptions(rootDir, key, consult),
        { intentKey: key },
      ),
      (error) => error.code === 'CONSULTATION_INTENT_BUSY',
    );
    release();
    await first;
    assert.equal(runnerCalls, 1);
  } finally {
    release?.();
    await removeRoot(rootDir);
  }
});
