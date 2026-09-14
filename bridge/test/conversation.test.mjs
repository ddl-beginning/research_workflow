import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  buildReceipt,
  CONVERSATION_MODES,
  consultOnce,
  deriveContinuationLineage,
  FAILURE_CODES,
  readContinuationReceipt,
  validateContinuationReceipt,
} from '../src/bridge.mjs';

const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';
const OTHER_CONVERSATION_ID = 'abcdefab-cdef-4abc-8def-abcdefabcdef';
const PARENT_ID = 'CONSULT-20260903-000000-aabbccdd';
const BRIDGE_ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));

function sampleReceipt({
  consultationId = PARENT_ID,
  mode = CONVERSATION_MODES.FRESH,
  conversationId = CONVERSATION_ID,
  parentConsultationId = null,
  conversationRootConsultationId = consultationId,
  conversationValidated = true,
  status = 'complete',
  chatUrl = `https://chatgpt.com/c/${conversationId}`,
} = {}) {
  return buildReceipt({
    consultationId,
    createdAt: '2026-09-03T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    chatUrl,
    mode,
    conversationId,
    parentConsultationId,
    conversationRootConsultationId,
    conversationValidated,
    status,
    responseCharCount: 8,
  });
}

async function createReceiptRoot(receipt = sampleReceipt()) {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-dialogue-'));
  const receiptDir = path.join(rootDir, '.consultations', receipt.consultation_id);
  await fs.mkdir(receiptDir, { recursive: true });
  await fs.writeFile(
    path.join(receiptDir, 'receipt.json'),
    `${JSON.stringify(receipt)}\n`,
    'utf8',
  );
  return { rootDir, receiptPath: path.join(receiptDir, 'receipt.json') };
}

async function removeTestRoot(rootDir) {
  await fs.rm(rootDir, { recursive: true, force: true });
}

function fakeBridge({
  initialUrl = 'https://chatgpt.com/',
   afterFreshUrl = `https://chatgpt.com/c/${CONVERSATION_ID}`,
  finalUrl,
  responseText = 'FAKE_OK',
} = {}) {
  const events = [];
  const bridge = {
    requestCount: 0,
    async open() {
      events.push('open');
    },
    async navigate() {
      events.push('navigate');
      return initialUrl;
    },
    async navigateTo(url) {
      events.push(`navigateTo:${url}`);
      return url;
    },
    async ensureLoggedIn() {
      events.push('ensureLoggedIn');
    },
    async waitForAssistantBaseline(options = {}) {
      events.push(`waitForAssistantBaseline:${options.requireNonEmpty ? 'non-empty' : 'empty-ok'}`);
      return options.requireNonEmpty
        ? [{ index: 0, id: 'existing-assistant', text: 'existing response' }]
        : [];
    },
    async createFreshConversation() {
      events.push('createFreshConversation');
      return { clicked: true, beforeUrl: initialUrl, afterUrl: afterFreshUrl };
    },
    async sendOnePrompt(prompt) {
      events.push(`send:${prompt}`);
      this.requestCount += 1;
      return responseText;
    },
    currentUrl() {
      return finalUrl || afterFreshUrl;
    },
    async close() {
      events.push('close');
    },
    releaseForManualLogin() {
      events.push('releaseForManualLogin');
    },
  };
  return { bridge, events };
}

test('fresh and continue receipts carry validated conversation lineage', () => {
  const fresh = sampleReceipt();
  assert.equal(validateContinuationReceipt(fresh, PARENT_ID), true);
  assert.deepEqual(deriveContinuationLineage(fresh), {
    parentConsultationId: PARENT_ID,
    conversationRootConsultationId: PARENT_ID,
    conversationId: CONVERSATION_ID,
  });

  const childId = 'CONSULT-20260903-000001-bbccddee';
  const continued = sampleReceipt({
    consultationId: childId,
    mode: CONVERSATION_MODES.CONTINUE,
    parentConsultationId: PARENT_ID,
    conversationRootConsultationId: PARENT_ID,
  });
  assert.equal(validateContinuationReceipt(continued, childId), true);
  assert.deepEqual(deriveContinuationLineage(continued), {
    parentConsultationId: childId,
    conversationRootConsultationId: PARENT_ID,
    conversationId: CONVERSATION_ID,
  });
});

test('continuation receipt lookup fails closed for missing, malformed, failed, and traversal inputs', async () => {
  const { rootDir } = await createReceiptRoot();
  try {
    await assert.rejects(
      readContinuationReceipt({ rootDir, consultationId: 'CONSULT-20260903-000000-deadbeef' }),
      (error) => error.code === FAILURE_CODES.CONTINUATION_RECEIPT_NOT_FOUND,
    );
    await assert.rejects(
      readContinuationReceipt({ rootDir, consultationId: '../CONSULT-20260903-000000-aabbccdd' }),
      (error) => error.code === FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
    );

    const failed = sampleReceipt({
      consultationId: 'CONSULT-20260903-000000-deadbeef',
      status: FAILURE_CODES.RESPONSE_TIMEOUT,
      conversationValidated: false,
    });
    const failedRoot = await createReceiptRoot(failed);
    try {
      await assert.rejects(
        readContinuationReceipt({ rootDir: failedRoot.rootDir, consultationId: failed.consultation_id }),
        (error) => error.code === FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      );
    } finally {
      await removeTestRoot(failedRoot.rootDir);
    }

    const malformed = sampleReceipt({ consultationId: 'CONSULT-20260903-000000-deadbeef' });
    const malformedRoot = await createReceiptRoot(malformed);
    await fs.writeFile(malformedRoot.receiptPath, '{not-json', 'utf8');
    try {
      await assert.rejects(
        readContinuationReceipt({ rootDir: malformedRoot.rootDir, consultationId: malformed.consultation_id }),
        (error) => error.code === FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      );
    } finally {
      await removeTestRoot(malformedRoot.rootDir);
    }
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('consultOnce fresh creates a new validated lineage with one request', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-fresh-'));
  const fake = fakeBridge({
    initialUrl: 'https://chatgpt.com/',
    afterFreshUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    finalUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    responseText: 'TURN_A_OK',
  });
  try {
    const result = await consultOnce('fresh prompt', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.FRESH,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(result.mode, CONVERSATION_MODES.FRESH);
    assert.equal(result.conversationId, CONVERSATION_ID);
    assert.equal(result.parentConsultationId, null);
    assert.equal(result.conversationRootConsultationId, result.consultationId);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.conversation_validated, true);
    assert.equal(receipt.conversation_id, CONVERSATION_ID);
    assert.equal(receipt.parent_consultation_id, null);
    assert.equal(receipt.conversation_root_consultation_id, result.consultationId);
    assert.equal(fake.events.filter((event) => event.startsWith('send:')).length, 1);
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('consultOnce fresh fails before the prompt when the new route has no conversation identity', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-fresh-route-timeout-'));
  const fake = fakeBridge({
    afterFreshUrl: 'https://chatgpt.com/',
    finalUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
  });
  try {
    await assert.rejects(
      consultOnce('fresh route timeout', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        mode: CONVERSATION_MODES.FRESH,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.FRESH_CHAT_CREATION_FAILED
        && error.requestCount === 0,
    );
    assert.equal(fake.bridge.requestCount, 0);
    assert.equal(fake.events.some((event) => event.startsWith('send:')), false);
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('consultOnce continue inherits parent conversation and sends one request', async () => {
  const parent = sampleReceipt();
  const { rootDir } = await createReceiptRoot(parent);
  const fake = fakeBridge({
    initialUrl: parent.chat_url,
    finalUrl: parent.chat_url,
    responseText: 'TURN_B_OK',
  });
  try {
    const result = await consultOnce('continue prompt', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.CONTINUE,
      continueFrom: parent.consultation_id,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(result.mode, CONVERSATION_MODES.CONTINUE);
    assert.equal(result.conversationId, parent.conversation_id);
    assert.equal(result.parentConsultationId, parent.consultation_id);
    assert.equal(result.conversationRootConsultationId, parent.conversation_root_consultation_id);
    assert.equal(fake.events.filter((event) => event.startsWith('send:')).length, 1);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.parent_consultation_id, parent.consultation_id);
    assert.equal(receipt.conversation_root_consultation_id, parent.conversation_root_consultation_id);
    assert.equal(receipt.conversation_id, parent.conversation_id);
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('consultOnce fails closed when fresh final identity changes unexpectedly', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-identity-'));
  const fake = fakeBridge({
    afterFreshUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    finalUrl: `https://chatgpt.com/c/${OTHER_CONVERSATION_ID}`,
  });
  try {
    await assert.rejects(
      consultOnce('identity prompt', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        mode: CONVERSATION_MODES.FRESH,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH,
    );
    assert.equal(fake.bridge.requestCount, 1);
  } finally {
    await removeTestRoot(rootDir);
  }
});
