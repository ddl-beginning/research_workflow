import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import {
  consultGptInputSchema,
  consultGptOutputSchema,
  createConsultGptHandler,
  MAX_TIMEOUT_MS,
  MIN_TIMEOUT_MS,
} from '../src/mcp-server.mjs';
import {
  CONVERSATION_MODES,
  FAILURE_CODES,
  MAX_RESPONSE_TIMEOUT_MS,
  MAX_CHATGPT_REQUESTS_PER_INVOCATION,
} from '../src/bridge.mjs';
import { BRIDGE_ROOT } from '../src/bridge.mjs';
import { buildContextPack } from '../src/context-pack.mjs';

test('consult_gpt input requires a non-empty prompt and rejects bridge controls', () => {
  const projectUrl = 'https://chatgpt.com/g/g-p-example-project/project';
  assert.equal(MAX_TIMEOUT_MS, MAX_RESPONSE_TIMEOUT_MS);
  assert.equal(MAX_TIMEOUT_MS, 300_000);
  assert.equal(consultGptInputSchema.safeParse({ prompt: '' }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: '   ' }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', profile_dir: '.auth/other' }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', timeout_ms: MIN_TIMEOUT_MS - 1 }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', timeout_ms: MAX_TIMEOUT_MS + 1 }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', timeout_ms: MAX_TIMEOUT_MS }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', timeout_ms: MIN_TIMEOUT_MS }).success, true);
  assert.equal(consultGptInputSchema.parse({ prompt: 'ok' }).mode, CONVERSATION_MODES.FRESH);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', mode: 'continue' }).success, false);
  assert.equal(consultGptInputSchema.safeParse({
    prompt: 'ok',
    mode: 'fresh',
    continue_from: 'CONSULT-20260903-000000-aabbccdd',
  }).success, false);
  assert.equal(consultGptInputSchema.safeParse({
    prompt: 'ok',
    mode: 'continue',
    continue_from: 'CONSULT-20260903-000000-aabbccdd',
  }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', chat_url: 'https://chatgpt.com/c/id' }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', project_url: projectUrl }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', project_url: 'https://evil.example/g/g-p-project/project' }).success, false);
});

test('consult_gpt returns complete response and receipt metadata from an injected adapter', async () => {
  const calls = [];
  const handler = createConsultGptHandler({
    consult: async (prompt, options) => {
      calls.push({ prompt, options });
      return {
        consultationId: 'CONSULT-test-success',
        responseText: 'BRIDGE_OK',
        receiptPath: 'D:/local/.consultations/CONSULT-test-success/receipt.json',
        requestCount: MAX_CHATGPT_REQUESTS_PER_INVOCATION,
      };
    },
  });

  const result = await handler({ prompt: 'Reply with exactly: BRIDGE_OK', timeout_ms: MIN_TIMEOUT_MS });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    prompt: 'Reply with exactly: BRIDGE_OK',
    options: { responseTimeoutMs: MIN_TIMEOUT_MS, mode: CONVERSATION_MODES.FRESH },
  });
  assert.equal(result.content[0].text, 'BRIDGE_OK');
  assert.deepEqual(result.structuredContent, {
    status: 'complete',
    consultation_id: 'CONSULT-test-success',
    response: 'BRIDGE_OK',
    receipt_path: 'D:/local/.consultations/CONSULT-test-success/receipt.json',
    request_count: 1,
  });
  assert.equal(consultGptOutputSchema.safeParse(result.structuredContent).success, true);
  assert.equal(result.isError, undefined);
});

test('consult_gpt forwards project_url without exposing bridge controls', async () => {
  const projectUrl = 'https://chatgpt.com/g/g-p-example-project/project';
  const calls = [];
  const handler = createConsultGptHandler({
    consult: async (prompt, options) => {
      calls.push({ prompt, options });
      return {
        consultationId: 'CONSULT-test-project',
        responseText: 'PROJECT_OK',
        receiptPath: 'D:/local/.consultations/CONSULT-test-project/receipt.json',
        requestCount: MAX_CHATGPT_REQUESTS_PER_INVOCATION,
      };
    },
  });
  const result = await handler({ prompt: 'project once', project_url: projectUrl });
  assert.equal(result.structuredContent.status, 'complete');
  assert.deepEqual(calls, [{
    prompt: 'project once',
    options: { mode: CONVERSATION_MODES.FRESH, projectUrl },
  }]);
});

test('consult_gpt preserves LOGIN_REQUIRED and timeout as structured tool errors', async () => {
  for (const code of [FAILURE_CODES.LOGIN_REQUIRED, FAILURE_CODES.RESPONSE_TIMEOUT]) {
    const handler = createConsultGptHandler({
      consult: async () => {
        const error = new Error(`${code} test`);
        error.code = code;
        error.consultationId = `CONSULT-${code}`;
        error.artifacts = { receiptPath: `D:/local/.consultations/CONSULT-${code}/receipt.json` };
        throw error;
      },
    });
    const result = await handler({ prompt: 'harmless test' });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, new RegExp(code));
    assert.deepEqual(result.structuredContent, {
      status: 'failed',
      consultation_id: `CONSULT-${code}`,
      response: '',
      receipt_path: `D:/local/.consultations/CONSULT-${code}/receipt.json`,
      request_count: 1,
      failure_code: code,
    });
    assert.equal(consultGptOutputSchema.safeParse(result.structuredContent).success, true);
  }
});

test('consult_gpt rejects an adapter result that violates the one-request budget', async () => {
  let calls = 0;
  const handler = createConsultGptHandler({
    consult: async () => {
      calls += 1;
      return {
        consultationId: 'CONSULT-invalid-budget',
        responseText: 'should not be accepted',
        receiptPath: 'D:/local/.consultations/CONSULT-invalid-budget/receipt.json',
        requestCount: 2,
      };
    },
  });
  const result = await handler({ prompt: 'budget test' });
  assert.equal(calls, 1);
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.status, 'failed');
  assert.equal(result.structuredContent.failure_code, FAILURE_CODES.UNEXPECTED_PAGE_STATE);
  assert.equal(result.structuredContent.request_count, MAX_CHATGPT_REQUESTS_PER_INVOCATION);
});

test('consult_gpt can load one explicitly named staged context packet', async () => {
  const packetId = 'PACK-mcp-test01';
  const packetDir = path.join(BRIDGE_ROOT, '.consultations', 'staging', packetId);
  const calls = [];
  try {
    await buildContextPack({
      rootDir: BRIDGE_ROOT,
      packetId,
      createdAt: '2026-09-04T00:00:00.000Z',
      projectGoal: 'Test goal',
      currentStageGoal: 'Test stage',
      latestResult: { actualWork: 'Test result' },
    });
    const handler = createConsultGptHandler({
      consult: async (prompt, options) => {
        calls.push({ prompt, options });
        return {
          consultationId: 'CONSULT-test-context-pack',
          responseText: 'PACK_OK',
          receiptPath: 'D:/local/.consultations/CONSULT-test-context-pack/receipt.json',
          requestCount: MAX_CHATGPT_REQUESTS_PER_INVOCATION,
        };
      },
    });
    const result = await handler({ prompt: 'Review the packet once.', context_pack_id: packetId });
    assert.equal(result.structuredContent.status, 'complete');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].options.contextPack.packetId, packetId);
    assert.equal(calls[0].options.contextPack.attachmentPaths.length, 2);
  } finally {
    await fs.rm(packetDir, { recursive: true, force: true });
  }
});
