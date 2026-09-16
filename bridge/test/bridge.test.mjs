import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  assertRequestBudget,
  BridgeError,
  buildReceipt,
  BROWSER_CHECKPOINT_STATUSES,
  ChatGPTBridge,
  CONVERSATION_MODES,
  DEFAULT_RESPONSE_TIMEOUT_MS,
  extractConversationIdFromUrl,
  FAILURE_CODES,
  HOME_NAVIGATION_RETRY_SETTLE_MS,
  MAX_HOME_NAVIGATION_RETRIES,
  MAX_RESPONSE_TIMEOUT_MS,
  MAX_CHATGPT_REQUESTS_PER_INVOCATION,
  normalizeResponseTimeoutMs,
  resolveResponseTimeoutMs,
  validateContinuationReceipt,
} from '../src/bridge.mjs';
import {
  extractNewAssistantResponse,
  normalizeAssistantSnapshot,
  DEFAULT_ASSISTANT_RESPONSE_TIMEOUT_MS,
  MAX_ASSISTANT_RESPONSE_TIMEOUT_MS,
  normalizeAssistantResponseTimeoutMs,
  validateStableAssistantSnapshot,
  waitForStableAssistantSnapshot,
  waitForStableAssistant,
  RESPONSE_FAILURE_CLASSES,
} from '../src/chatgpt-ui.mjs';

test('default response wait is three minutes with a five-minute cap and timeout receipts stay classified', () => {
  assert.equal(DEFAULT_RESPONSE_TIMEOUT_MS, 180_000);
  assert.equal(MAX_RESPONSE_TIMEOUT_MS, 300_000);
  assert.equal(DEFAULT_ASSISTANT_RESPONSE_TIMEOUT_MS, DEFAULT_RESPONSE_TIMEOUT_MS);
  assert.equal(MAX_ASSISTANT_RESPONSE_TIMEOUT_MS, MAX_RESPONSE_TIMEOUT_MS);
  assert.equal(normalizeResponseTimeoutMs(), DEFAULT_RESPONSE_TIMEOUT_MS);
  assert.equal(normalizeResponseTimeoutMs(String(MAX_RESPONSE_TIMEOUT_MS)), MAX_RESPONSE_TIMEOUT_MS);
  assert.equal(resolveResponseTimeoutMs(String(MAX_RESPONSE_TIMEOUT_MS)), MAX_RESPONSE_TIMEOUT_MS);
  assert.throws(
    () => normalizeResponseTimeoutMs(MAX_RESPONSE_TIMEOUT_MS + 1),
    (error) => error instanceof BridgeError && error.code === FAILURE_CODES.UNEXPECTED_PAGE_STATE,
  );
  assert.throws(
    () => resolveResponseTimeoutMs(String(MAX_RESPONSE_TIMEOUT_MS + 1)),
    (error) => error instanceof BridgeError && error.code === FAILURE_CODES.UNEXPECTED_PAGE_STATE,
  );
  assert.throws(
    () => new ChatGPTBridge({ responseTimeoutMs: MAX_RESPONSE_TIMEOUT_MS + 1 }),
    (error) => error instanceof BridgeError && error.code === FAILURE_CODES.UNEXPECTED_PAGE_STATE,
  );
  assert.throws(
    () => normalizeAssistantResponseTimeoutMs(MAX_ASSISTANT_RESPONSE_TIMEOUT_MS + 1),
    (error) => error instanceof RangeError,
  );
  const bridge = new ChatGPTBridge();
  assert.equal(bridge.responseTimeoutMs, DEFAULT_RESPONSE_TIMEOUT_MS);
  const receipt = buildReceipt({
    consultationId: 'CONSULT-20260904-000000-aabbccdd',
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.CONTINUE,
    status: FAILURE_CODES.RESPONSE_TIMEOUT,
    failureCode: FAILURE_CODES.RESPONSE_TIMEOUT,
    requestCount: 1,
  });
  assert.equal(receipt.status, FAILURE_CODES.RESPONSE_TIMEOUT);
  assert.equal(receipt.failure_code, FAILURE_CODES.RESPONSE_TIMEOUT);
  assert.equal(receipt.request_count, 1);
});

test('one invocation permits exactly one ChatGPT request', () => {
  assert.equal(MAX_CHATGPT_REQUESTS_PER_INVOCATION, 1);
  assert.doesNotThrow(() => assertRequestBudget(1));
  assert.throws(
    () => assertRequestBudget(2),
    (error) => error instanceof BridgeError && error.code === FAILURE_CODES.UNEXPECTED_PAGE_STATE,
  );
});

test('homepage navigation retries one transient failure without changing the prompt budget', async () => {
  const bridge = new ChatGPTBridge({ navigationTimeoutMs: 10 });
  bridge.context = {};
  let gotoCount = 0;
  const waits = [];
  bridge.page = {
    async goto() {
      gotoCount += 1;
      if (gotoCount <= MAX_HOME_NAVIGATION_RETRIES) throw new Error('transient browser transport failure');
    },
    async waitForTimeout(ms) { waits.push(ms); },
    url() { return 'https://chatgpt.com/'; },
  };

  assert.equal(await bridge.navigate(), 'https://chatgpt.com/');
  assert.equal(MAX_HOME_NAVIGATION_RETRIES, 2);
  assert.equal(gotoCount, MAX_HOME_NAVIGATION_RETRIES + 1);
  assert.deepEqual(waits, [HOME_NAVIGATION_RETRY_SETTLE_MS, HOME_NAVIGATION_RETRY_SETTLE_MS * 2, 750]);
  assert.equal(bridge.requestCount, 0);
});

test('extractor selects only one new stable assistant message', () => {
  const baseline = ['turn-1'];
  const snapshot = [
    { id: 'turn-1', text: 'old response' },
    { id: 'turn-2', text: 'TEST_A\nTEST_B\nTEST_C' },
  ];
  assert.deepEqual(extractNewAssistantResponse(snapshot, baseline), snapshot[1]);
  assert.equal(extractNewAssistantResponse(snapshot.slice(0, 1), baseline), null);
  assert.throws(() => extractNewAssistantResponse([
    ...snapshot,
    { id: 'turn-3', text: 'unexpected extra turn' },
  ], baseline));
});

test('extractor refuses missing stable assistant identifiers', () => {
  assert.throws(() => extractNewAssistantResponse([
    { id: 'turn-1', text: 'old' },
    { id: '', text: 'new' },
  ], ['turn-1']));
});

test('snapshot normalization ignores hidden duplicate nodes and keeps one visible logical turn', () => {
  const snapshot = normalizeAssistantSnapshot([
    { visible: true, id: 'turn-1', slotId: 'conversation-turn-2', text: 'old response' },
    { visible: false, id: 'hidden-copy', slotId: 'conversation-turn-2', text: 'old response' },
    { visible: true, id: 'turn-2', slotId: 'conversation-turn-4', text: 'new response' },
  ]);

  assert.deepEqual(snapshot, [
    { index: 0, id: 'turn-1', slotId: 'conversation-turn-2', text: 'old response' },
    { index: 1, id: 'turn-2', slotId: 'conversation-turn-4', text: 'new response' },
  ]);
  assert.deepEqual(snapshot.duplicateSlotIds, ['conversation-turn-2']);
  assert.equal(snapshot.visibleNodeCount, 2);
  assert.doesNotThrow(() => validateStableAssistantSnapshot(snapshot));
});

test('snapshot normalization fails closed for conflicting visible nodes in one logical turn', () => {
  assert.throws(
    () => normalizeAssistantSnapshot([
      { visible: true, id: 'placeholder-id', slotId: 'conversation-turn-6', text: 'partial' },
      { visible: true, id: 'final-id', slotId: 'conversation-turn-6', text: 'complete' },
    ]),
    /conflict within one logical turn/i,
  );
});

test('baseline waits for hydrated history and stable IDs before returning', async () => {
  const snapshots = [
    [],
    [],
    [
      { index: 0, id: 'old-1', text: '' },
      { index: 1, id: 'old-2', text: 'loaded history' },
    ],
    [
      { index: 0, id: 'old-1', text: 'loaded first history' },
      { index: 1, id: 'old-2', text: 'loaded history' },
    ],
    [
      { index: 0, id: 'old-1', text: 'loaded first history' },
      { index: 1, id: 'old-2', text: 'loaded history' },
    ],
    [
      { index: 0, id: 'old-1', text: 'loaded first history' },
      { index: 1, id: 'old-2', text: 'loaded history' },
    ],
  ];
  let index = 0;
  const baseline = await waitForStableAssistantSnapshot(
    async () => snapshots[Math.min(index++, snapshots.length - 1)],
    {
      requireNonEmpty: true,
      timeoutMs: 250,
      pollMs: 1,
      stabilityMs: 2,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );
  assert.deepEqual(baseline, snapshots.at(-1));
});

test('completion tolerates a streaming placeholder ID replacement in one slot', async () => {
  const snapshots = [
    [{ index: 0, id: 'request-placeholder', text: 'partial' }],
    [],
    [{ index: 0, id: 'final-message-id', text: 'DIAGNOSTIC_OK' }],
    [{ index: 0, id: 'final-message-id', text: 'DIAGNOSTIC_OK' }],
    [{ index: 0, id: 'final-message-id', text: 'DIAGNOSTIC_OK' }],
  ];
  let snapshotIndex = 0;
  let generationChecks = 0;
  const response = await waitForStableAssistant(
    async () => snapshots[Math.min(snapshotIndex++, snapshots.length - 1)],
    async () => generationChecks++ === 0,
    [],
    {
      baselineCount: 0,
      timeoutMs: 200,
      pollMs: 1,
      stabilityMs: 2,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );
  assert.equal(response, 'DIAGNOSTIC_OK');
});

test('completion reloads once when a stopped empty assistant placeholder needs hydration', async () => {
  const snapshots = [
    [{ index: 0, id: 'new-placeholder', slotId: 'conversation-turn-new', text: '' }],
    [{ index: 0, id: 'new-placeholder', slotId: 'conversation-turn-new', text: '' }],
    [{ index: 0, id: 'hydrated-response', slotId: 'conversation-turn-new', text: 'RELOADED_RESPONSE_OK' }],
    [{ index: 0, id: 'hydrated-response', slotId: 'conversation-turn-new', text: 'RELOADED_RESPONSE_OK' }],
    [{ index: 0, id: 'hydrated-response', slotId: 'conversation-turn-new', text: 'RELOADED_RESPONSE_OK' }],
  ];
  let snapshotIndex = 0;
  let generationChecks = 0;
  let refreshCount = 0;
  const response = await waitForStableAssistant(
    async () => snapshots[Math.min(snapshotIndex++, snapshots.length - 1)],
    async () => generationChecks++ === 0,
    [],
    {
      timeoutMs: 200,
      pollMs: 1,
      stabilityMs: 2,
      refreshDelayMs: 0,
      refresh: async () => {
        refreshCount += 1;
      },
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );

  assert.equal(response, 'RELOADED_RESPONSE_OK');
  assert.equal(refreshCount, 1);
});

test('hydration reload restores the saved Project conversation route after root redirect', async () => {
  const conversationId = '12345678-1234-4234-8234-123456789abc';
  const savedRoute = `https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/c/${conversationId}`;
  let currentUrl = savedRoute;
  let reloadCount = 0;
  const gotoCalls = [];
  const bridge = new ChatGPTBridge({ navigationTimeoutMs: 10_000 });
  bridge.context = {};
  bridge.page = {
    url: () => currentUrl,
    async reload() {
      reloadCount += 1;
      currentUrl = 'https://chatgpt.com/';
    },
    async goto(url) {
      gotoCalls.push(url);
      currentUrl = url;
    },
    async waitForTimeout() {},
  };

  assert.equal(await bridge.reloadConversationForHydration(), true);
  assert.equal(reloadCount, 1);
  assert.deepEqual(gotoCalls, [savedRoute]);
  assert.equal(currentUrl, savedRoute);
});

test('completion refreshes an active empty assistant placeholder after a bounded delay', async () => {
  const snapshots = [
    [{ index: 0, id: 'active-placeholder', slotId: 'conversation-turn-active', text: '' }],
    [{ index: 0, id: 'hydrated-active', slotId: 'conversation-turn-active', text: 'ACTIVE_HYDRATION_OK' }],
    [{ index: 0, id: 'hydrated-active', slotId: 'conversation-turn-active', text: 'ACTIVE_HYDRATION_OK' }],
    [{ index: 0, id: 'hydrated-active', slotId: 'conversation-turn-active', text: 'ACTIVE_HYDRATION_OK' }],
  ];
  let snapshotIndex = 0;
  let generationChecks = 0;
  let refreshCount = 0;
  const response = await waitForStableAssistant(
    async () => snapshots[Math.min(snapshotIndex++, snapshots.length - 1)],
    async () => generationChecks++ === 0,
    [],
    {
      timeoutMs: 200,
      pollMs: 1,
      stabilityMs: 2,
      activeRefreshDelayMs: 0,
      refresh: async () => { refreshCount += 1; },
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );

  assert.equal(response, 'ACTIVE_HYDRATION_OK');
  assert.equal(refreshCount, 1);
});

test('completion detects same-count assistant identity hydration replacement', async () => {
  const baseline = [
    { index: 0, id: 'placeholder-id', slotId: 'conversation-turn-2', text: '' },
  ];
  const snapshots = [
    baseline,
    [{ index: 0, id: 'final-id', slotId: 'conversation-turn-2', text: 'SAME_COUNT_REPLACEMENT_OK' }],
    [{ index: 0, id: 'final-id', slotId: 'conversation-turn-2', text: 'SAME_COUNT_REPLACEMENT_OK' }],
    [{ index: 0, id: 'final-id', slotId: 'conversation-turn-2', text: 'SAME_COUNT_REPLACEMENT_OK' }],
  ];
  let snapshotIndex = 0;
  const response = await waitForStableAssistant(
    async () => snapshots[Math.min(snapshotIndex++, snapshots.length - 1)],
    async () => false,
    baseline.map((item) => item.id),
    {
      baselineCount: baseline.length,
      baselineSnapshot: baseline,
      timeoutMs: 200,
      pollMs: 1,
      stabilityMs: 2,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );

  assert.equal(response, 'SAME_COUNT_REPLACEMENT_OK');
});

test('same-count hydration timeout is classified as an incomplete assistant turn', async () => {
  const baseline = [
    { index: 0, id: 'placeholder-id', slotId: 'conversation-turn-2', text: '' },
  ];
  const replacement = [
    { index: 0, id: 'final-id', slotId: 'conversation-turn-2', text: 'PARTIAL_SAME_COUNT_RESPONSE' },
  ];
  let snapshotIndex = 0;
  await assert.rejects(
    waitForStableAssistant(
      async () => {
        snapshotIndex += 1;
        return snapshotIndex === 1 ? baseline : replacement;
      },
      async () => true,
      baseline.map((item) => item.id),
      {
        baselineCount: baseline.length,
        baselineSnapshot: baseline,
        timeoutMs: 8,
        pollMs: 1,
        stabilityMs: 1,
        sleep: async () => {},
      },
    ),
    (error) => {
      assert.equal(error.responseForensic.new_assistant_count, 1);
      assert.equal(error.responseForensic.response_failure_class, RESPONSE_FAILURE_CLASSES.ASSISTANT_TURN_APPEARED_NOT_COMPLETED);
      assert.notEqual(error.responseForensic.response_failure_class, RESPONSE_FAILURE_CLASSES.NO_NEW_ASSISTANT_TURN);
      return true;
    },
  );
});

test('completion accepts exactly one new logical assistant slot', async () => {
  const baseline = [
    { index: 0, id: 'old-id', slotId: 'conversation-turn-2', text: 'old response' },
  ];
  const snapshots = [
    baseline,
    [
      ...baseline,
      { index: 1, id: 'new-id', slotId: 'conversation-turn-4', text: 'NEW_SLOT_OK' },
    ],
    [
      ...baseline,
      { index: 1, id: 'new-id', slotId: 'conversation-turn-4', text: 'NEW_SLOT_OK' },
    ],
    [
      ...baseline,
      { index: 1, id: 'new-id', slotId: 'conversation-turn-4', text: 'NEW_SLOT_OK' },
    ],
  ];
  let snapshotIndex = 0;
  const response = await waitForStableAssistant(
    async () => snapshots[Math.min(snapshotIndex++, snapshots.length - 1)],
    async () => false,
    baseline.map((item) => item.id),
    {
      baselineCount: baseline.length,
      timeoutMs: 200,
      pollMs: 1,
      stabilityMs: 2,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    },
  );

  assert.equal(response, 'NEW_SLOT_OK');
});

test('hard timeout never returns partial assistant text', async () => {
  const snapshots = [
    [{ id: 'turn-1', text: 'old' }],
    [{ id: 'turn-1', text: 'old' }, { id: 'turn-2', text: 'partial' }],
  ];
  let index = 0;
  await assert.rejects(
    waitForStableAssistant(
      async () => snapshots[Math.min(index++, snapshots.length - 1)],
      async () => true,
      ['turn-1'],
      { baselineCount: 1, timeoutMs: 8, pollMs: 1, stabilityMs: 1, sleep: async () => {} },
    ),
    (error) => error instanceof Error && error.message.startsWith('Timed out'),
  );
});

test('response forensic timeout is bounded and classifies missing assistant turn', async () => {
  await assert.rejects(
    waitForStableAssistant(
      async () => [{ index: 0, id: 'old-turn', slotId: 'conversation-turn-old', text: 'old' }],
      async () => false,
      ['old-turn'],
      { baselineCount: 1, timeoutMs: 4, pollMs: 1, stabilityMs: 1, sleep: async () => {} },
    ),
    (error) => {
      assert.equal(error.responseForensic.response_failure_class, RESPONSE_FAILURE_CLASSES.NO_NEW_ASSISTANT_TURN);
      assert.equal(error.responseForensic.baseline_assistant_count, 1);
      assert.equal(error.responseForensic.new_assistant_count, 0);
      assert.match(error.responseForensic.candidates[0].text_sha256, /^[0-9a-f]{64}$/);
      assert.doesNotMatch(JSON.stringify(error.responseForensic), /old-turn|conversation-turn-old|old/);
      return true;
    },
  );
});

test('response forensic timeout classifies a streaming assistant turn as incomplete', async () => {
  const snapshots = [
    [{ index: 0, id: 'old-turn', slotId: 'conversation-turn-old', text: 'old' }],
    [
      { index: 0, id: 'old-turn', slotId: 'conversation-turn-old', text: 'old' },
      { index: 1, id: 'new-turn', slotId: 'conversation-turn-new', text: 'partial response' },
    ],
  ];
  let index = 0;
  await assert.rejects(
    waitForStableAssistant(
      async () => snapshots[Math.min(index++, snapshots.length - 1)],
      async () => true,
      ['old-turn'],
      { baselineCount: 1, timeoutMs: 4, pollMs: 1, stabilityMs: 1, sleep: async () => {} },
    ),
    (error) => {
      assert.equal(error.responseForensic.response_failure_class, RESPONSE_FAILURE_CLASSES.ASSISTANT_TURN_APPEARED_NOT_COMPLETED);
      assert.equal(error.responseForensic.new_assistant_count, 1);
      assert.equal(error.responseForensic.streaming_seen, true);
      assert.equal(error.responseForensic.generation_active, true);
      assert.equal(error.responseForensic.candidates.at(-1).is_new, true);
      return true;
    },
  );
});

test('response forensic identity conflict records a precise rejection invariant', async () => {
  await assert.rejects(
    waitForStableAssistant(
      async () => [
        { index: 0, id: 'new-a', slotId: 'conversation-turn-new-a', text: 'a' },
        { index: 1, id: 'new-b', slotId: 'conversation-turn-new-b', text: 'b' },
      ],
      async () => false,
      [],
      { baselineCount: 0, timeoutMs: 20, pollMs: 1, stabilityMs: 1, sleep: async () => {} },
    ),
    (error) => {
      assert.equal(error.responseForensic.response_failure_class, RESPONSE_FAILURE_CLASSES.ASSISTANT_IDENTITY_HYDRATION_CONFLICT);
      assert.equal(error.responseForensic.reject_invariant, 'multiple_new_assistant_messages');
      assert.equal(error.responseForensic.new_assistant_count, 2);
      return true;
    },
  );
});

test('receipt response forensic metadata is bounded and excludes raw identifiers/text', () => {
  const receipt = buildReceipt({
    consultationId: 'CONSULT-20260904-000000-aabbccdd',
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.CONTINUE,
    status: FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
    failureCode: FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
    requestCount: 1,
    responseForensic: {
      response_failure_class: RESPONSE_FAILURE_CLASSES.CHATGPT_UI_RATE_LIMIT,
      reject_invariant: 'response_deadline',
      baseline_assistant_count: 2,
      baseline_assistant_id_hashes: ['a'.repeat(16), 'raw-id-should-drop'],
      latest_message_id_hash: 'b'.repeat(64),
      latest_text_length: 9,
      latest_text_sha256: 'c'.repeat(64),
      empty_assistant_refresh_trigger: 'active_generation',
      candidates: [{
        logical_slot_hash: 'd'.repeat(16),
        message_id_hash: 'e'.repeat(16),
        visible: true,
        is_new: true,
        text_length: 9,
        text_sha256: 'f'.repeat(64),
        streaming: false,
        completion: false,
        raw_text: 'password=do-not-store',
        raw_id: 'C:\\Users\\secret',
      }],
    },
  });
  assert.equal(receipt.response_forensic.response_failure_class, RESPONSE_FAILURE_CLASSES.CHATGPT_UI_RATE_LIMIT);
  assert.deepEqual(receipt.response_forensic.baseline_assistant_id_hashes, ['a'.repeat(16)]);
  assert.equal(receipt.response_forensic.candidates.length, 1);
  assert.equal(receipt.response_forensic.empty_assistant_refresh_trigger, 'active_generation');
  assert.doesNotMatch(JSON.stringify(receipt), /raw-id|password|do-not-store|Users|secret/i);
});

test('receipt contains only non-secret consultation metadata', () => {
  const consultationId = 'CONSULT-20260903-000000-aabbccdd';
  const conversationId = '12345678-1234-4234-8234-123456789abc';
  const receipt = buildReceipt({
    consultationId,
    createdAt: '2026-09-03T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    conversationId,
    parentConsultationId: null,
    conversationRootConsultationId: consultationId,
    conversationValidated: true,
    chatUrl: `https://chatgpt.com/c/${conversationId}`,
    status: 'complete',
    responseCharCount: 8,
  });
  assert.deepEqual(Object.keys(receipt).sort(), [
    'chat_url',
    'consultation_id',
    'created_at',
    'conversation_id',
    'conversation_root_consultation_id',
    'conversation_validated',
    'chatgpt_project_target_verified',
    'chatgpt_target_mode',
    'chatgpt_target_origin',
    'chatgpt_target_url_digest',
    'fresh_project_chat_created',
    'mode',
    'parent_consultation_id',
    'profile',
    'request_count',
    'response_char_count',
    'status',
  ].sort());
  assert.doesNotMatch(JSON.stringify(receipt), /password|cookie|token|storage|secret/i);
  assert.equal(extractConversationIdFromUrl(receipt.chat_url), conversationId);
  assert.equal(validateContinuationReceipt(receipt, consultationId), true);
});

test('receipt stores bounded browser checkpoints and stable failure taxonomy', () => {
  const receipt = buildReceipt({
    consultationId: 'CONSULT-20260914-000000-aabbccdd',
    createdAt: '2026-09-14T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    status: 'failed_before_prompt',
    failureCode: FAILURE_CODES.ATTACHMENT_NOT_READY,
    browserCheckpoints: [
      { checkpoint: 'B0', status: BROWSER_CHECKPOINT_STATUSES.PASS },
      { checkpoint: 'B99', status: BROWSER_CHECKPOINT_STATUSES.PASS },
      {
        checkpoint: 'B7',
        status: BROWSER_CHECKPOINT_STATUSES.FAIL,
        failure_class: 'ATTACHMENT_NOT_READY',
        raw_dom: 'must-not-appear',
      },
    ],
  });
  assert.deepEqual(receipt.browser_checkpoints, [
    { checkpoint: 'B0', status: 'PASS' },
    { checkpoint: 'B7', status: 'FAIL', failure_class: 'ATTACHMENT_NOT_READY' },
  ]);
  assert.equal(receipt.failure_class, 'ATTACHMENT_NOT_READY');
  assert.doesNotMatch(JSON.stringify(receipt), /must-not-appear|raw_dom/i);
});
