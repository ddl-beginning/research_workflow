import crypto from 'node:crypto';

// Keep direct UI-helper callers consistent with the bridge response budget.
// Callers may provide a shorter explicit test timeout, but never extend the
// single-request wait beyond the bridge's five-minute cap.
export const DEFAULT_ASSISTANT_RESPONSE_TIMEOUT_MS = 180_000;
export const MAX_ASSISTANT_RESPONSE_TIMEOUT_MS = 300_000;
// New-chat navigation is a client-side route transition and can settle after
// the click promise resolves. Keep the wait bounded while allowing the route
// to expose its conversation identity before the bridge sends a prompt.
export const DEFAULT_NEW_CHAT_ROUTE_TIMEOUT_MS = 10_000;
export const MAX_NEW_CHAT_ROUTE_TIMEOUT_MS = 15_000;
export const NEW_CHAT_ROUTE_POLL_MS = 50;
// On Project routes the first assistant node can remain an empty streaming
// placeholder while the SPA hydrates, even when the generation indicator is
// still active. A short bounded delay lets normal streaming start before one
// navigation-only reload hydrates the persisted response.
export const ACTIVE_EMPTY_ASSISTANT_REFRESH_DELAY_MS = 10_000;
export const MAX_ACTIVE_EMPTY_ASSISTANT_REFRESH_DELAY_MS = 30_000;
export const EMPTY_ASSISTANT_REFRESH_DELAY_MS = 2_000;
export const MAX_EMPTY_ASSISTANT_REFRESH_DELAY_MS = 10_000;
// A mounted Project composer can accept a FileList before its attachment
// uploader has subscribed to the input event. Give that first binding a
// bounded hydration window, then re-find the active composer input once and
// reattach the same files while the invocation is still pre-prompt.
export const ATTACHMENT_REATTACH_AFTER_MS = 10_000;
export const ATTACHMENT_REATTACH_SETTLE_MS = 750;
export const MAX_ATTACHMENT_REATTACH_ATTEMPTS = 1;

export function normalizeAssistantResponseTimeoutMs(value = DEFAULT_ASSISTANT_RESPONSE_TIMEOUT_MS) {
  const timeoutMs = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_ASSISTANT_RESPONSE_TIMEOUT_MS) {
    throw new RangeError(
      `Assistant response timeout must be a finite positive number no greater than ${MAX_ASSISTANT_RESPONSE_TIMEOUT_MS}ms.`,
    );
  }
  return timeoutMs;
}

export const ASSISTANT_MESSAGE_SELECTOR = '[data-message-author-role="assistant"]';
const USER_MESSAGE_SELECTOR = '[data-message-author-role="user"]';
const ASSISTANT_TURN_SELECTOR = 'section[data-testid^="conversation-turn-"]';
const MAX_STABLE_ANCESTOR_DEPTH = 12;
const MAX_RESPONSE_DIAGNOSTIC_ITEMS = 16;
const MAX_RESPONSE_DIAGNOSTIC_CANDIDATES = 64;

const RESPONSE_BANNER_SELECTOR = [
  '[role="alert"]',
  '[aria-live="assertive"]',
  '[data-testid*="toast" i]',
  '[data-testid*="error" i]',
  '[data-testid*="banner" i]',
  '[class*="toast" i]',
  '[class*="banner" i]',
].join(', ');

const RESPONSE_RETRY_BUTTON_SELECTOR = 'button';

export const RESPONSE_FAILURE_CLASSES = Object.freeze({
  NO_NEW_ASSISTANT_TURN: 'NO_NEW_ASSISTANT_TURN',
  ASSISTANT_TURN_APPEARED_NOT_COMPLETED: 'ASSISTANT_TURN_APPEARED_NOT_COMPLETED',
  ASSISTANT_RESPONSE_VISIBLE_EXTRACTOR_REJECTED: 'ASSISTANT_RESPONSE_VISIBLE_EXTRACTOR_REJECTED',
  ASSISTANT_IDENTITY_HYDRATION_CONFLICT: 'ASSISTANT_IDENTITY_HYDRATION_CONFLICT',
  CHATGPT_UI_RATE_LIMIT: 'CHATGPT_UI_RATE_LIMIT',
  CHATGPT_UI_ERROR: 'CHATGPT_UI_ERROR',
  CHATGPT_UI_ERROR_STATE: 'CHATGPT_UI_ERROR_STATE',
  UNKNOWN_RESPONSE_FAILURE: 'UNKNOWN_RESPONSE_FAILURE',
});

function sha256DiagnosticValue(value) {
  return crypto.createHash('sha256').update(String(value ?? ''), 'utf8').digest('hex');
}

function diagnosticHash(value) {
  if (typeof value !== 'string' || !value) return null;
  return sha256DiagnosticValue(value).slice(0, 16);
}

function boundedDiagnosticList(values, mapper = (value) => value) {
  if (!Array.isArray(values)) return [];
  const result = [];
  const seen = new Set();
  for (const value of values) {
    const mapped = mapper(value);
    if (typeof mapped !== 'string' || !mapped || seen.has(mapped)) continue;
    seen.add(mapped);
    result.push(mapped);
    if (result.length >= MAX_RESPONSE_DIAGNOSTIC_ITEMS) break;
  }
  return result;
}

function safeDiagnosticInteger(value, fallback = null) {
  return Number.isInteger(value) && value >= 0 ? value : fallback;
}

function snapshotDiagnosticRecords(snapshot) {
  return Array.isArray(snapshot)
    ? snapshot.filter((item) => item && typeof item === 'object')
    : [];
}

function snapshotDiagnosticSummary(snapshot, baselineIds = []) {
  const records = snapshotDiagnosticRecords(snapshot);
  const baseline = new Set(Array.isArray(baselineIds) ? baselineIds.filter((id) => typeof id === 'string') : []);
  const newRecords = records.filter((item) => typeof item.id === 'string' && !baseline.has(item.id));
  const idHashes = boundedDiagnosticList(records, (item) => diagnosticHash(item.id));
  const slotHashes = boundedDiagnosticList(records, (item) => diagnosticHash(item.slotId || item.id));
  const newIdHashes = boundedDiagnosticList(newRecords, (item) => diagnosticHash(item.id));
  const newSlotHashes = boundedDiagnosticList(newRecords, (item) => diagnosticHash(item.slotId || item.id));
  const duplicateSlotHashes = boundedDiagnosticList(snapshot?.duplicateSlotIds, (slotId) => diagnosticHash(slotId));
  const textSummaries = records.slice(0, MAX_RESPONSE_DIAGNOSTIC_ITEMS).map((item) => ({
    length: safeDiagnosticInteger(typeof item.text === 'string' ? item.text.length : null),
    sha256: typeof item.text === 'string' ? sha256DiagnosticValue(item.text) : null,
  }));
  return {
    snapshotCount: records.length,
    visibleNodeCount: Number.isInteger(snapshot?.visibleNodeCount) ? snapshot.visibleNodeCount : records.length,
    baselineCount: Array.isArray(baselineIds) ? baselineIds.length : 0,
    newCount: newRecords.length,
    idHashes,
    slotHashes,
    newIdHashes,
    newSlotHashes,
    duplicateSlotHashes,
    textSummaries,
  };
}

const COMPOSER_SELECTORS = [
  (page) => page.getByPlaceholder(/message chatgpt|send a message|问问\s*chatgpt|消息/i),
  (page) => page.getByRole('textbox', { name: /message|prompt|chat|问问|聊天|消息|与\s*chatgpt\s*聊天/i }),
  (page) => page.locator('textarea'),
  (page) => page.locator('[contenteditable="true"]'),
];

const SEND_SELECTORS = [
  (page) => page.getByRole('button', { name: /^(send|send message|submit|发送|发送消息|提交)$/i }),
  (page) => page.locator('[data-testid="send-button"]'),
  (page) => page.locator('button[aria-label*="Send" i], button[aria-label*="发送"]'),
];

const NEW_CHAT_SELECTORS = [
  (page) => page.getByRole('button', {
    name: /^(new chat|new conversation|start new chat|新聊天|新对话|开始新聊天)$/i,
  }),
  (page) => page.getByRole('link', {
    name: /^(new chat|new conversation|start new chat|新聊天|新对话|开始新聊天)$/i,
  }),
  (page) => page.locator('[data-testid="create-new-chat-button"], [data-testid="new-chat"], [data-testid="new-chat-button"], [data-testid="new-conversation"]'),
];

const CHATGPT_CONVERSATION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function extractConversationIdFromUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.origin !== 'https://chatgpt.com') return null;
  const segments = parsed.pathname.split('/').filter(Boolean);
  if (segments.length !== 2 || segments[0] !== 'c' || !CHATGPT_CONVERSATION_ID_PATTERN.test(segments[1])) {
    return null;
  }
  return segments[1];
}

function boundedNewChatTimeout(value, fallback) {
  const timeoutMs = Number(value);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) return fallback;
  return Math.min(timeoutMs, MAX_NEW_CHAT_ROUTE_TIMEOUT_MS);
}

function boundedNewChatPoll(value) {
  const pollMs = Number(value);
  if (!Number.isFinite(pollMs) || pollMs < 0) return NEW_CHAT_ROUTE_POLL_MS;
  return pollMs;
}

async function waitForFreshConversationRoute(
  page,
  beforeUrl,
  { timeoutMs = DEFAULT_NEW_CHAT_ROUTE_TIMEOUT_MS, pollMs = NEW_CHAT_ROUTE_POLL_MS } = {},
) {
  const beforeConversationId = extractConversationIdFromUrl(beforeUrl);
  const deadline = Date.now() + boundedNewChatTimeout(timeoutMs, DEFAULT_NEW_CHAT_ROUTE_TIMEOUT_MS);
  const intervalMs = boundedNewChatPoll(pollMs);
  let afterUrl = beforeUrl;

  while (true) {
    afterUrl = page.url();
    const conversationId = extractConversationIdFromUrl(afterUrl);
    if (conversationId && conversationId !== beforeConversationId) {
      return { afterUrl, conversationId };
    }
    if (Date.now() >= deadline) {
      return { afterUrl, conversationId: null };
    }

    const remainingMs = Math.max(0, deadline - Date.now());
    const waitMs = Math.min(intervalMs, remainingMs);
    if (typeof page.waitForTimeout === 'function') {
      await page.waitForTimeout(waitMs);
    } else {
      await new Promise((resolve) => setTimeout(resolve, waitMs));
    }
  }
}

const LOGIN_SELECTORS = [
  (page) => page.getByRole('button', { name: /^(log in|sign up|login|登录|注册)$/i }).first(),
  (page) => page.getByRole('link', { name: /^(log in|sign up|login|登录|注册)$/i }).first(),
];

// Text-only login copy is not a reliable signal: authenticated Project pages
// can contain the same words in rendered navigation/help content.  Keep URL
// detection narrow and limited to the trusted ChatGPT origin instead.
const LOGIN_ROUTE_PATTERN = /^\/(?:auth\/)?(?:login|sign[-_]up|signup)\/?$/i;

function isChatGptLoginUrl(page) {
  if (!page || typeof page.url !== 'function') return false;
  try {
    const currentUrl = new URL(page.url());
    return currentUrl.origin === 'https://chatgpt.com'
      && LOGIN_ROUTE_PATTERN.test(currentUrl.pathname);
  } catch {
    return false;
  }
}

const STOP_SELECTORS = [
  (page) => page.getByRole('button', { name: /stop generating|stop response|stop|停止生成|停止回复|停止/i }),
  (page) => page.locator('[data-testid="stop-button"]'),
  (page) => page.locator('button[aria-label*="Stop" i], button[aria-label*="停止"]'),
];

// Attachment selectors live in this module so that bridge orchestration never
// needs to know about ChatGPT's DOM.  The UI has changed these attributes over
// time, therefore each selector is deliberately small and semantic; readiness
// is decided from several independent DOM signals below rather than a sleep.
export const ATTACHMENT_FILE_INPUT_SELECTOR = 'input[type="file"]';
const COMPOSER_ROOT_SELECTOR = 'form[data-type="unified-composer"]';
const COMPOSER_ATTACHMENT_FILE_INPUT_SELECTOR = `${COMPOSER_ROOT_SELECTOR} ${ATTACHMENT_FILE_INPUT_SELECTOR}`;
const ATTACHMENT_BUTTON_SELECTORS = [
  (page) => page.getByRole('button', { name: /attach files?|upload files?|add files?|添加附件|上传文件|上传附件/i }),
  (page) => page.locator('[data-testid*="attach" i], [data-testid*="upload" i]'),
  (page) => page.locator('button[aria-label*="attach" i], button[aria-label*="upload" i], button[aria-label*="添加附件"], button[aria-label*="上传"]'),
];
const ATTACHMENT_TILE_SELECTOR = [
  'form[data-type="unified-composer"] [role="group"][aria-label]',
  'form[data-type="unified-composer"] [data-attachment-id]',
  'form[data-type="unified-composer"] [data-file-id]',
  'form[data-type="unified-composer"] [role="listitem"][aria-label*="file" i]',
  'form[data-type="unified-composer"] [role="listitem"][aria-label*="attachment" i]',
].join(', ');
const ATTACHMENT_PENDING_SELECTOR = [
  'form[data-type="unified-composer"] [role="group"][aria-label] button.cursor-wait',
  'form[data-type="unified-composer"] [role="group"][aria-label] [class*="cursor-wait"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-busy="true"]',
  'form[data-type="unified-composer"] [role="group"][aria-label][data-state="loading"]',
  'form[data-type="unified-composer"] [role="group"][aria-label][data-status="uploading"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [data-state="loading"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [data-status="uploading"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-label*="uploading" i]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-label*="上传中"]',
].join(', ');
const ATTACHMENT_ERROR_SELECTOR = [
  'form[data-type="unified-composer"] [role="group"][aria-label][data-state="error"]',
  'form[data-type="unified-composer"] [role="group"][aria-label][data-status="error"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [data-state="error"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [data-status="error"]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-label*="upload failed" i]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-label*="attachment error" i]',
  'form[data-type="unified-composer"] [role="group"][aria-label] [aria-label*="上传失败"]',
].join(', ');
const ATTACHMENT_PROGRESS_SELECTOR = [
  `${COMPOSER_ROOT_SELECTOR} [role="progressbar"]`,
  `${COMPOSER_ROOT_SELECTOR} progress`,
  `${COMPOSER_ROOT_SELECTOR} [aria-valuenow]`,
  `${COMPOSER_ROOT_SELECTOR} [data-progress]`,
  `${COMPOSER_ROOT_SELECTOR} [data-upload-progress]`,
].join(', ');
const ATTACHMENT_REMOVE_SELECTOR = [
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="remove attachment" i]`,
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="remove file" i]`,
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="delete attachment" i]`,
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="移除文件"]`,
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="删除附件"]`,
  `${COMPOSER_ROOT_SELECTOR} button[aria-label*="移除附件"]`,
  `${COMPOSER_ROOT_SELECTOR} [data-testid*="remove-attachment" i]`,
  `${COMPOSER_ROOT_SELECTOR} [data-testid*="remove-file" i]`,
].join(', ');

// `ATTACHMENT_TILE_SELECTOR` is intentionally broad enough to survive small
// ChatGPT DOM changes.  Its result is therefore a candidate set, not proof
// that every candidate is an attachment chip.  The browser-side reader adds
// semantic signals to each candidate before the readiness predicate is
// allowed to treat a basename as contradictory.

async function firstAvailable(page, selectorFactories) {
  for (const factory of selectorFactories) {
    try {
      const locator = factory(page);
      if (await locator.count()) return locator.first();
    } catch {
      // The composer can rerender while an upload control is being mounted.
    }
  }
  return null;
}

async function readVisibleCount(locator) {
  try {
    const total = await locator.count();
    let count = 0;
    for (let index = 0; index < total; index += 1) {
      if (await locator.nth(index).isVisible()) count += 1;
    }
    return { count, reliable: true };
  } catch {
    // A concurrent rerender is not equivalent to zero pending/error nodes.
    return { count: 0, reliable: false };
  }
}

async function readVisibleSelectorCount(page, selector) {
  try {
    return await readVisibleCount(page.locator(selector));
  } catch {
    return { count: 0, reliable: false };
  }
}

async function readAttachmentProgressState(page) {
  try {
    const records = await page.locator(ATTACHMENT_PROGRESS_SELECTOR).evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const visible = !element.hidden
        && element.getAttribute('aria-hidden') !== 'true'
        && style.display !== 'none'
        && style.visibility !== 'hidden'
        && Number(style.opacity) !== 0
        && rect.width > 0
        && rect.height > 0;
      const status = [
        element.getAttribute('data-state'),
        element.getAttribute('data-status'),
        element.getAttribute('aria-label'),
      ].filter(Boolean).join(' ').toLowerCase();
      const now = Number(element.getAttribute('aria-valuenow'));
      const max = Number(element.getAttribute('aria-valuemax'));
      const completed = status.includes('complete')
        || status.includes('ready')
        || (Number.isFinite(now) && Number.isFinite(max) && max > 0 && now >= max);
      return { visible, completed };
    }).filter((record) => record.visible));
    return {
      present: records.length > 0,
      completed: records.length > 0 && records.every((record) => record.completed),
      reliable: true,
    };
  } catch {
    return { present: false, completed: false, reliable: false };
  }
}

async function countVisibleStrict(locator) {
  const total = await locator.count();
  let count = 0;
  for (let index = 0; index < total; index += 1) {
    if (await locator.nth(index).isVisible()) count += 1;
  }
  return count;
}

function sanitizeAttachmentAccept(value) {
  if (typeof value !== 'string') return null;
  const tokens = value
    .split(',')
    .map((token) => token.trim().toLowerCase())
    .filter((token) => /^(?:\.[a-z0-9][a-z0-9+*-]{0,31}|[a-z0-9][a-z0-9+*-]{0,31}\/[a-z0-9][a-z0-9+*.-]{0,31})$/.test(token));
  if (tokens.length === 0) return null;
  return tokens.slice(0, 16).join(',').slice(0, 128);
}

function emptyAttachmentFileInputState() {
  return {
    locator: null,
    totalCount: 0,
    selectedIndex: null,
    owner: 'none',
    accept: null,
    multiple: null,
    composerScopedCount: 0,
    reliable: true,
  };
}

/**
 * Select the file input owned by the active unified composer when available.
 * ChatGPT can leave hidden upload inputs from an old composer/modal in the
 * document; selecting the first global input can therefore report FileList
 * success without producing a composer tile.  The fallback is intentionally
 * conservative and is used only when no composer-scoped input exists.
 */
async function inspectAttachmentFileInputs(page) {
  const state = emptyAttachmentFileInputState();
  let globalInputs;
  try {
    globalInputs = page.locator(ATTACHMENT_FILE_INPUT_SELECTOR);
    state.totalCount = await globalInputs.count();
  } catch {
    state.reliable = false;
    return state;
  }

  try {
    state.composerScopedCount = await page.locator(COMPOSER_ATTACHMENT_FILE_INPUT_SELECTOR).count();
  } catch {
    state.reliable = false;
  }

  const records = [];
  for (let index = 0; index < state.totalCount; index += 1) {
    const locator = globalInputs.nth(index);
    let metadata;
    try {
      metadata = await locator.evaluate((element) => {
        const form = element.closest('form[data-type="unified-composer"]');
        const visible = (node) => {
          if (!node || !node.isConnected) return false;
          const style = getComputedStyle(node);
          return !node.hidden
            && node.getAttribute('aria-hidden') !== 'true'
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && Number(style.opacity) !== 0;
        };
        return {
          owner: form ? 'unified-composer' : 'global-fallback',
          active: Boolean(form && visible(form)),
          accept: element.getAttribute('accept') || '',
          multiple: Boolean(element.multiple),
        };
      });
    } catch {
      metadata = { owner: 'unknown', active: false, accept: '', multiple: null };
    }
    if (!metadata || typeof metadata !== 'object') {
      metadata = { owner: 'unknown', active: false, accept: '', multiple: null };
    }
    records.push({ index, locator, metadata });
  }

  // Prefer an active composer input. If a DOM variant has more than one
  // active candidate, the last one is the most recently mounted composer.
  const activeComposer = records.filter((record) => (
    record.metadata.owner === 'unified-composer' && record.metadata.active === true
  )).at(-1);
  // A scoped input with no visible form is not safe to select. Returning no
  // input lets setAttachmentFiles use the explicit chooser fallback instead
  // of silently targeting a stale hidden control.
  const selected = activeComposer || (state.composerScopedCount === 0 ? records[0] : null);
  if (!selected) return state;

  state.locator = selected.locator;
  state.selectedIndex = selected.index;
  state.owner = selected.metadata.owner === 'unified-composer'
    ? 'unified-composer'
    : 'global-fallback';
  state.accept = sanitizeAttachmentAccept(selected.metadata.accept);
  state.multiple = typeof selected.metadata.multiple === 'boolean' ? selected.metadata.multiple : null;
  return state;
}

export async function readAttachmentFileInputState(page) {
  const state = await inspectAttachmentFileInputs(page);
  return {
    totalCount: state.totalCount,
    selectedIndex: state.selectedIndex,
    owner: state.owner,
    accept: state.accept,
    multiple: state.multiple,
    composerScopedCount: state.composerScopedCount,
    reliable: state.reliable,
    locator: state.locator,
  };
}

export async function findAttachmentFileInput(page) {
  const state = await readAttachmentFileInputState(page);
  return state.locator;
}

async function inspectFirstVisible(page, selectorFactories) {
  let reliable = true;
  for (const factory of selectorFactories) {
    try {
      const locator = factory(page);
      const count = await locator.count();
      for (let index = 0; index < count; index += 1) {
        const candidate = locator.nth(index);
        if (await candidate.isVisible()) return { locator: candidate, reliable };
      }
    } catch {
      reliable = false;
    }
  }
  return { locator: null, reliable };
}

export async function findAttachmentButton(page) {
  return firstInteractive(page, ATTACHMENT_BUTTON_SELECTORS);
}

export async function firstVisible(page, selectorFactories) {
  for (const factory of selectorFactories) {
    try {
      const locator = factory(page);
      const count = await locator.count();
      for (let index = 0; index < count; index += 1) {
        const candidate = locator.nth(index);
        if (await candidate.isVisible()) return candidate;
      }
    } catch {
      // ChatGPT can rerender between count and visibility; try the next stable selector.
    }
  }
  return null;
}

async function firstInteractive(page, selectorFactories) {
  for (const factory of selectorFactories) {
    try {
      const locator = factory(page);
      const count = await locator.count();
      for (let index = 0; index < count; index += 1) {
        const candidate = locator.nth(index);
        if (!await candidate.isVisible()) continue;
        const pointerEvents = await candidate.evaluate((element) => getComputedStyle(element).pointerEvents);
        if (pointerEvents === 'none') continue;
        return candidate;
      }
    } catch {
      // ChatGPT can rerender between count, visibility, and computed style checks.
    }
  }
  return null;
}

export async function findComposer(page) {
  return firstVisible(page, COMPOSER_SELECTORS);
}

export async function findSendButton(page) {
  return firstVisible(page, SEND_SELECTORS);
}

function normalizeAttachmentBasename(value) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  if (!trimmed || trimmed.includes('\0')) return '';
  // The UI normally exposes the basename directly in aria-label/data-file-name.
  // Strip a path only as a defensive fallback for a browser-provided title.
  const basename = trimmed.replace(/^.*[\\/]/, '');
  return basename.replace(/\((?:\d+|\d{8}-\d{6})\)(?=\.[^.]+$)/, '');
}

function normalizeAttachmentBasenameList(values) {
  if (!Array.isArray(values)) return [];
  return values
    .map(normalizeAttachmentBasename)
    .filter(Boolean);
}

function boundedAttachmentBasenameList(values, limit = 32) {
  return Array.from(new Set(normalizeAttachmentBasenameList(values)))
    .slice(0, limit)
    .map((name) => name.slice(0, 128));
}

function normalizeExpectedBasenames(expectedBasenames) {
  return Array.from(new Set(
    normalizeAttachmentBasenameList(expectedBasenames),
  ));
}

function sameAttachmentBasenameMultiset(actual, expected) {
  const actualNames = normalizeAttachmentBasenameList(actual);
  const expectedNames = normalizeAttachmentBasenameList(expected);
  if (actualNames.length !== expectedNames.length) return false;
  const counts = new Map();
  for (const name of expectedNames) counts.set(name, (counts.get(name) || 0) + 1);
  for (const name of actualNames) {
    const remaining = counts.get(name);
    if (!remaining) return false;
    if (remaining === 1) counts.delete(name);
    else counts.set(name, remaining - 1);
  }
  return counts.size === 0;
}

const ATTACHMENT_UI_LABELS = new Set([
  'remove',
  'remove file',
  'remove attachment',
  'delete',
  'delete file',
  'delete attachment',
  'download',
  'download file',
  'cancel',
  '移除',
  '移除文件',
  '移除附件',
  '删除',
  '删除文件',
  '删除附件',
  '下载',
  '取消',
]);

const ATTACHMENT_CONTRADICTION_REASONS = Object.freeze({
  NONE: 'none',
  VISIBLE_WRONG_ATTACHMENT_BASENAME: 'visible_wrong_attachment_basename',
});

function attachmentRecordBoolean(record, keys) {
  return keys.some((key) => record?.[key] === true);
}

function attachmentRecordBasenames(record) {
  if (!record || typeof record !== 'object') return [];
  const values = [];
  for (const key of [
    'basenameCandidates',
    'attachmentBasenameCandidates',
    'basenames',
    'names',
    'candidates',
    'basename',
    'filename',
    'fileName',
    'attachmentBasename',
  ]) {
    const value = record[key];
    if (Array.isArray(value)) values.push(...value);
    else if (typeof value === 'string') values.push(value);
  }
  return normalizeAttachmentBasenameList(values);
}

function normalizeAttachmentTileRecord(record) {
  if (!record || typeof record !== 'object') return null;
  const supplementary = attachmentRecordBoolean(record, [
    'summary',
    'isSummary',
    'virtualized',
    'isVirtualized',
    'historical',
    'isHistorical',
    'supplementary',
  ]);
  const hasAttachmentOperation = attachmentRecordBoolean(record, [
    'hasAttachmentOperation',
    'attachmentOperation',
    'hasRemoveOrUploadControl',
  ]);
  const hasUploadStatus = attachmentRecordBoolean(record, [
    'hasUploadStatus',
    'attachmentStatus',
    'uploadStatus',
    'hasAttachmentStatus',
  ]);
  const hasAttachmentSemantics = attachmentRecordBoolean(record, [
    'hasAttachmentSemantics',
    'attachmentSemantic',
    'semantic',
  ]) || hasAttachmentOperation || hasUploadStatus;
  return {
    // Unknown visibility is deliberately not eligible.  The browser reader
    // only emits `visible: true`; this also keeps hand-built/legacy states
    // supplementary instead of turning them into a false contradiction.
    visible: record.visible === true,
    attachmentContainer: !supplementary && attachmentRecordBoolean(record, [
      'attachmentContainer',
      'isAttachmentContainer',
      'attachmentChip',
      'isAttachmentChip',
    ]),
    hasAttachmentOperation,
    hasUploadStatus,
    hasAttachmentSemantics,
    supplementary,
    basenames: attachmentRecordBasenames(record),
  };
}

function looksLikeAttachmentBasename(name) {
  if (typeof name !== 'string' || !name) return false;
  // Tile candidates also include aria labels/text for controls and summary
  // UI. Only a file-like basename from a semantically identified attachment
  // container is strong enough to contradict FileList evidence.
  return /^[^./\\\r\n]+\.[^./\\\s]{1,64}$/.test(name);
}

function attachmentBasenameContradictionSummary(values, expectedNames) {
  const expected = new Set(normalizeAttachmentBasenameList(expectedNames));
  const records = Array.isArray(values)
    ? values.map(normalizeAttachmentTileRecord).filter(Boolean)
    : [];
  let visibleCandidateCount = 0;
  let semanticCandidateCount = 0;
  let contradictionCount = 0;
  const contradictionNames = new Set();
  for (const record of records) {
    if (!record.visible) continue;
    visibleCandidateCount += 1;
    // A candidate must be both a clear attachment container and carry an
    // attachment operation/upload-state signal.  Plain page text, summaries,
    // virtualized placeholders, and historical content remain inconclusive.
    if (record.supplementary || !record.attachmentContainer || !record.hasAttachmentSemantics) continue;
    semanticCandidateCount += 1;
    const wrongNames = record.basenames.filter((name) => {
      const normalized = name.toLowerCase().replace(/\s+/g, ' ');
      return looksLikeAttachmentBasename(name)
        && !ATTACHMENT_UI_LABELS.has(normalized)
        && !expected.has(name);
    });
    if (wrongNames.length === 0) continue;
    contradictionCount += 1;
    for (const name of wrongNames) contradictionNames.add(name);
  }
  return {
    visibleCandidateCount,
    semanticCandidateCount,
    contradictionCount,
    contradictionBasenames: Array.from(contradictionNames),
    reason: contradictionCount > 0
      ? ATTACHMENT_CONTRADICTION_REASONS.VISIBLE_WRONG_ATTACHMENT_BASENAME
      : ATTACHMENT_CONTRADICTION_REASONS.NONE,
    hasContradiction: contradictionCount > 0,
  };
}

export function hasVisibleAttachmentBasenameContradiction(values, expectedNames) {
  return attachmentBasenameContradictionSummary(values, expectedNames).hasContradiction;
}

function exactAttachmentNameSet(values) {
  const names = new Set();
  for (const value of values) {
    if (typeof value !== 'string') continue;
    // A text fallback may contain a remove button label on another line. Each
    // line is treated as one candidate; no substring matching is performed.
    for (const line of value.split(/\r?\n/)) {
      const name = normalizeAttachmentBasename(line);
      if (name) names.add(name);
    }
  }
  return names;
}

export async function readAttachmentUploadState(page, { expectedBasenames = [] } = {}) {
  let readReliable = true;
  const fileInputState = await readAttachmentFileInputState(page);
  const fileInputs = fileInputState.locator;
  readReliable = readReliable && fileInputState.reliable;
  const fileInputPresent = Boolean(fileInputs);
  let inputFileCount = 0;
  let inputFileBasenames = [];
  if (fileInputs) {
    try {
      const selection = await fileInputs.evaluate((element) => {
        const count = element.files?.length || 0;
        const basenames = Array.from(element.files || [], (file) => (
          typeof file?.name === 'string' ? file.name : ''
        ));
        return { count, basenames };
      });
      if (Number.isInteger(selection)) {
        // Keep compatibility with narrow offline fakes that expose only a
        // numeric FileList length; real browser inputs return the object form.
        inputFileCount = selection;
      } else if (selection && typeof selection === 'object') {
        inputFileCount = Number.isInteger(selection.count) ? selection.count : 0;
        inputFileBasenames = normalizeAttachmentBasenameList(selection.basenames);
      } else {
        readReliable = false;
      }
    } catch {
      inputFileCount = 0;
      inputFileBasenames = [];
      readReliable = false;
    }
  }

  let tileRecords = [];
  try {
    tileRecords = await page.locator(ATTACHMENT_TILE_SELECTOR).evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const operationPattern = /(?:^|\s)(?:remove|delete|cancel|download)(?:\s|$)|(?:remove|delete|cancel|download)\s+(?:file|attachment)|(?:file|attachment)\s+(?:remove|delete|cancel|download)|移除(?:文件|附件)?|删除(?:文件|附件)?|取消(?:上传|文件|附件)?|下载(?:文件|附件)?/i;
      const statusPattern = /(?:^|\s)(?:upload(?:ing|ed)?|processing|pending|ready|complete|completed|error|failed)(?:\s|$)|上传(?:中|完成|失败)?|处理(?:中|完成|失败)?|就绪|完成|失败/i;
      const value = (node, attribute) => node.getAttribute(attribute) || '';
      const ownAttributes = [
        value(element, 'data-file-name'),
        value(element, 'data-filename'),
        value(element, 'data-name'),
      ].filter(Boolean);
      const descendants = Array.from(element.querySelectorAll('*'));
      const controls = [
        ...Array.from(element.querySelectorAll('button, [role="button"]')),
      ];
      const controlLabels = controls.map((control) => [
        value(control, 'aria-label'),
        value(control, 'title'),
        value(control, 'data-testid'),
        control.innerText || '',
      ].filter(Boolean).join(' '));
      const descendantAttributes = descendants.flatMap((descendant) => [
        value(descendant, 'data-file-name'),
        value(descendant, 'data-filename'),
        value(descendant, 'data-name'),
        value(descendant, 'aria-label'),
        value(descendant, 'title'),
      ].filter(Boolean));
      const descendantFilenameAttributes = descendants.flatMap((descendant) => [
        value(descendant, 'data-file-name'),
        value(descendant, 'data-filename'),
        value(descendant, 'data-name'),
      ].filter(Boolean));
      const hasAttachmentOperation = controlLabels.some((label) => operationPattern.test(label));
      const statusNodes = [element, ...descendants];
      const hasUploadStatus = statusNodes.some((node) => {
        const state = value(node, 'data-state');
        const status = value(node, 'data-status');
        const busy = value(node, 'aria-busy').toLowerCase();
        const progress = [
          value(node, 'aria-valuenow'),
          value(node, 'data-progress'),
          value(node, 'data-upload-progress'),
        ].some(Boolean);
        const labels = [value(node, 'aria-label'), value(node, 'title')].filter(Boolean);
        const labelledUploadStatus = labels.some((label) => (
          /(?:upload|attach|attachment|file|上传|附件|文件)/i.test(label)
          && statusPattern.test(label)
        ));
        return progress
          || busy === 'true'
          || statusPattern.test(state)
          || statusPattern.test(status)
          || labelledUploadStatus;
      }) || Boolean(element.querySelector('[role="progressbar"], progress, [aria-valuenow], [data-progress], [data-upload-progress]'));
      const role = (value(element, 'role') || '').toLowerCase();
      const hasAttachmentId = Boolean(value(element, 'data-attachment-id') || value(element, 'data-file-id'));
      const hasFilenameAttribute = ownAttributes.length > 0 || descendantFilenameAttributes.length > 0;
      const labelledContainer = /(?:^|\s)(?:file|attachment|upload|document)(?:\s|$)|文件|附件|上传/i.test([
        value(element, 'aria-label'),
        value(element, 'title'),
      ].filter(Boolean).join(' '));
      const filenamePattern = /^[^./\\\r\n]+\.[^./\\\s]{1,64}$/;
      const ownLabels = [value(element, 'aria-label'), value(element, 'title')].filter(Boolean);
      const accessibleBasenameCandidates = [
        ...ownLabels,
        ...descendants.flatMap((descendant) => [
          value(descendant, 'aria-label'),
          value(descendant, 'title'),
        ].filter(Boolean)),
      ].filter((candidate) => (
        filenamePattern.test(candidate.trim())
        // ChatGPT labels the remove affordance with the file basename, e.g.
        // "移除文件1：report.txt".  That is an operation label, not a second
        // attachment name and must not trigger wrong-basename rejection.
        && !operationPattern.test(candidate.trim())
      ));
      const textBasenameCandidates = (element.innerText || '')
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => filenamePattern.test(line));
      // A visible "N files"/"N attachments" summary can contain the names
      // of many historical or virtualized items.  It is useful candidate text
      // for diagnostics/matching, but never a basename source for a single
      // contradiction.
      const summaryText = [element.innerText || '', ...ownLabels].join(' ');
      const isSummary = /(?:^|\s)(?:\d+\s*)?(?:files?|attachments?|documents?)(?:\s|$)|(?:文件|附件)(?:列表|数量)?|\b\d+\s*(?:of|\/)\s*\d+\b|\b(?:showing|selected|more|others?)\b/i.test(summaryText);
      const attachmentContainer = hasAttachmentId
        || ((role === 'group' || role === 'listitem')
          && (hasFilenameAttribute || labelledContainer || hasAttachmentOperation || hasUploadStatus));
      const candidates = [
        ...ownAttributes,
        value(element, 'aria-label'),
        value(element, 'title'),
        element.innerText || '',
      ];
      for (const descendant of element.querySelectorAll('[data-file-name], [data-filename], [data-name], [aria-label], [title]')) {
        candidates.push(
          descendant.getAttribute('data-file-name') || '',
          descendant.getAttribute('data-filename') || '',
          descendant.getAttribute('data-name') || '',
          descendant.getAttribute('aria-label') || '',
          descendant.getAttribute('title') || '',
        );
      }
      return {
        visible: !element.hidden
          && element.getAttribute('aria-hidden') !== 'true'
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && Number(style.opacity) !== 0
          && rect.width > 0
          && rect.height > 0,
        candidates,
        // Keep a separate basename channel for contradiction checks.  The
        // ordinary candidate list intentionally still includes visible text so
        // expected-chip matching remains backwards compatible, while only
        // semantically identified containers may use these names to reject.
        basenameCandidates: [
          ...ownAttributes,
          ...(!isSummary ? descendantFilenameAttributes : []),
          ...(!isSummary ? accessibleBasenameCandidates : []),
          ...(!isSummary ? textBasenameCandidates : []),
        ].filter(Boolean),
        attachmentContainer,
        hasAttachmentOperation,
        hasUploadStatus,
        supplementary: isSummary,
      };
    }));
  } catch {
    tileRecords = [];
    readReliable = false;
  }
  const visibleTiles = tileRecords.filter((tile) => tile && tile.visible === true);
  const pendingState = await readVisibleSelectorCount(page, ATTACHMENT_PENDING_SELECTOR);
  const errorState = await readVisibleSelectorCount(page, ATTACHMENT_ERROR_SELECTOR);
  const progressState = await readAttachmentProgressState(page);
  readReliable = readReliable && pendingState.reliable && errorState.reliable && progressState.reliable;
  const pendingCount = pendingState.count;
  const errorCount = errorState.count;
  const composerState = await inspectFirstVisible(page, COMPOSER_SELECTORS);
  readReliable = readReliable && composerState.reliable;
  const composer = composerState.locator;
  let composerReady = Boolean(composer);
  if (composer) {
    try {
      const state = await composer.evaluate((element) => ({
        disabled: Boolean(element.disabled),
        ariaDisabled: element.getAttribute('aria-disabled') === 'true',
      }));
      const enabled = typeof composer.isEnabled === 'function' ? await composer.isEnabled() : true;
      composerReady = !state.disabled && !state.ariaDisabled && enabled;
    } catch {
      composerReady = false;
      readReliable = false;
    }
  }

  const sendState = await inspectFirstVisible(page, SEND_SELECTORS);
  readReliable = readReliable && sendState.reliable;
  const send = sendState.locator;
  let sendControlPresent = Boolean(send);
  let sendAvailable = false;
  if (send) {
    try {
      const state = await send.evaluate((element) => ({
        disabled: Boolean(element.disabled),
        ariaDisabled: element.getAttribute('aria-disabled') === 'true',
      }));
      const enabled = typeof send.isEnabled === 'function' ? await send.isEnabled() : true;
      sendAvailable = !state.disabled && !state.ariaDisabled && enabled;
    } catch {
      sendAvailable = false;
      sendControlPresent = false;
      readReliable = false;
    }
  }

  const expectedNames = normalizeExpectedBasenames(expectedBasenames);
  const visibleTileNames = exactAttachmentNameSet(
    visibleTiles.flatMap((tile) => Array.isArray(tile.candidates) ? tile.candidates : []),
  );
  const names = expectedNames.filter((name) => visibleTileNames.has(name));
  const attachmentTileRecords = visibleTiles.map((tile) => ({
    visible: tile.visible === true,
    // Do not carry raw innerText or DOM attributes out of evaluateAll.  The
    // normalized basename channel is bounded again when diagnostics are
    // serialized, and is used only for the semantic contradiction check.
    basenameCandidates: boundedAttachmentBasenameList(
      Array.isArray(tile.basenameCandidates)
        ? tile.basenameCandidates
        : tile.candidates,
    ),
    attachmentContainer: tile.attachmentContainer === true,
    hasAttachmentOperation: tile.hasAttachmentOperation === true,
    hasUploadStatus: tile.hasUploadStatus === true,
    hasAttachmentSemantics: tile.hasAttachmentSemantics === true
      || tile.hasAttachmentOperation === true
      || tile.hasUploadStatus === true,
    supplementary: tile.supplementary === true,
  }));
  const contradictionSummary = attachmentBasenameContradictionSummary(
    attachmentTileRecords,
    expectedNames,
  );

  /*
   * The active composer input's FileList provides exact bounded names/count,
   * but it only proves that Chromium accepted a local selection. It does not
   * prove that ChatGPT finished processing it. Pending/error/progress state
   * and the usable composer/send controls provide the completion signals;
   * visible tile names are supplementary because the UI may virtualize them.
   */
  return {
    fileInputPresent,
    inputFileCount,
    fileInputTotalCount: fileInputState.totalCount,
    fileInputSelectedIndex: fileInputState.selectedIndex,
    fileInputOwner: fileInputState.owner,
    fileInputAccept: fileInputState.accept,
    fileInputMultiple: fileInputState.multiple,
    composerScopedInputCount: fileInputState.composerScopedCount,
    inputFileBasenames,
    chipCount: visibleTiles.length,
    pendingCount,
    errorCount,
    composerReady,
    sendControlPresent,
    sendAvailable,
    progressPresent: progressState.present,
    progressCompleted: progressState.completed,
    readReliable,
    tileBasenames: Array.from(visibleTileNames),
    matchedBasenames: names,
    // Candidate metadata is deliberately structural and bounded to semantic
    // booleans plus normalized basenames; it never contains DOM/text dumps.
    attachmentTileRecords,
    attachmentCandidateCount: contradictionSummary.visibleCandidateCount,
    attachmentSemanticCount: contradictionSummary.semanticCandidateCount,
    attachmentContradictionCount: contradictionSummary.contradictionCount,
    attachmentContradictionReason: contradictionSummary.reason,
  };
}

export function attachmentReadinessDiagnostics(
  state,
  expectedCount,
  expectedBasenames = [],
  { elapsedMs = 0, finalPredicate = false, mode = undefined, requestCount = undefined } = {},
) {
  const expectedNames = normalizeExpectedBasenames(expectedBasenames);
  const expectedNameList = normalizeAttachmentBasenameList(expectedBasenames);
  const inputFileBasenames = normalizeAttachmentBasenameList(state?.inputFileBasenames);
  const matchedCount = Array.isArray(state?.matchedBasenames)
    ? new Set(state.matchedBasenames.map(normalizeAttachmentBasename).filter(Boolean)).size
    : 0;
  const contradictionSummary = attachmentBasenameContradictionSummary(
    state?.attachmentTileRecords,
    expectedNames,
  );
  const visibleAttachmentCandidateCount = Array.isArray(state?.attachmentTileRecords)
    ? contradictionSummary.visibleCandidateCount
    : safeDiagnosticInteger(state?.attachmentCandidateCount, safeDiagnosticInteger(state?.chipCount, 0));
  const visibleAttachmentSemanticCount = Array.isArray(state?.attachmentTileRecords)
    ? contradictionSummary.semanticCandidateCount
    : safeDiagnosticInteger(state?.attachmentSemanticCount, 0);
  const visibleAttachmentContradictionCount = Array.isArray(state?.attachmentTileRecords)
    ? contradictionSummary.contradictionCount
    : safeDiagnosticInteger(state?.attachmentContradictionCount, 0);
  const visibleAttachmentContradictionReason = visibleAttachmentContradictionCount > 0
    ? ATTACHMENT_CONTRADICTION_REASONS.VISIBLE_WRONG_ATTACHMENT_BASENAME
    : ATTACHMENT_CONTRADICTION_REASONS.NONE;
  const diagnostics = {
    requested_count: Number.isInteger(expectedCount) ? expectedCount : null,
    file_input_present: state?.fileInputPresent === true,
    file_input_count: Number.isInteger(state?.inputFileCount) ? state.inputFileCount : null,
    file_input_accepted: state?.fileInputPresent === true && Number.isInteger(expectedCount)
      ? state.inputFileCount === expectedCount
      : null,
    file_input_total_count: safeDiagnosticInteger(state?.fileInputTotalCount),
    file_input_selected_index: safeDiagnosticInteger(state?.fileInputSelectedIndex),
    file_input_owner: ['unified-composer', 'global-fallback', 'none'].includes(state?.fileInputOwner)
      ? state.fileInputOwner
      : null,
    file_input_accept: sanitizeAttachmentAccept(state?.fileInputAccept),
    file_input_multiple: typeof state?.fileInputMultiple === 'boolean' ? state.fileInputMultiple : null,
    composer_scoped_input_count: safeDiagnosticInteger(state?.composerScopedInputCount),
    file_input_basename_count: Array.isArray(state?.inputFileBasenames) ? inputFileBasenames.length : null,
    file_input_basenames_matched: expectedNameList.length === 0
      ? null
      : sameAttachmentBasenameMultiset(inputFileBasenames, expectedNameList),
    visible_chip_count: Number.isInteger(state?.chipCount) ? state.chipCount : null,
    expected_basename_count: expectedNames.length,
    matched_basename_count: matchedCount,
    expected_basenames_matched: expectedNames.length === 0 ? null : matchedCount === expectedNames.length,
    visible_attachment_candidate_count: visibleAttachmentCandidateCount,
    visible_attachment_semantic_count: visibleAttachmentSemanticCount,
    visible_attachment_contradiction_count: visibleAttachmentContradictionCount,
    attachment_contradiction_reason: visibleAttachmentContradictionReason,
    pending_count: Number.isInteger(state?.pendingCount) ? state.pendingCount : null,
    progress_present: state?.progressPresent === true,
    progress_completed: state?.progressPresent === true ? state.progressCompleted === true : null,
    error_ui: Number.isInteger(state?.errorCount) ? state.errorCount > 0 : null,
    error_count: Number.isInteger(state?.errorCount) ? state.errorCount : null,
    composer_ready: state?.composerReady === true,
    send_control_present: state?.sendControlPresent === true,
    send_enabled: state?.sendAvailable === true,
    read_reliable: state?.readReliable === true,
    final_predicate: finalPredicate === true,
    elapsed_wait_ms: Number.isFinite(elapsedMs) ? Math.max(0, Math.round(elapsedMs)) : null,
  };
  // Basenames are the only per-file UI detail needed to distinguish a
  // missing upload from a readiness-detector false negative. They are
  // sanitized, bounded display names; never persist paths, file contents, or
  // browser/session state.
  const boundedNames = (values) => Array.from(new Set(
    (Array.isArray(values) ? values : [])
      .map(normalizeAttachmentBasename)
      .filter(Boolean)
      .slice(0, 16),
  )).map((name) => name.slice(0, 128));
  diagnostics.expected_basenames = boundedNames(expectedNames);
  diagnostics.file_input_basenames = boundedNames(inputFileBasenames);
  diagnostics.tile_basenames = boundedNames(state?.tileBasenames);
  diagnostics.matched_basenames = boundedNames(state?.matchedBasenames);
  if (typeof mode === 'string' && mode) diagnostics.mode = mode;
  if (Number.isInteger(requestCount) && requestCount >= 0) diagnostics.request_count = requestCount;
  return diagnostics;
}

export function isAttachmentUploadReady(state, expectedCount, expectedBasenames = []) {
  if (!state || !Number.isInteger(expectedCount) || expectedCount <= 0) return false;
  if (state.readReliable !== true) return false;
  if (!Array.isArray(state.tileBasenames) || !Array.isArray(state.matchedBasenames)) return false;
  if (state.fileInputPresent !== true || state.fileInputOwner !== 'unified-composer') return false;
  if (!Number.isInteger(state.inputFileCount) || state.inputFileCount !== expectedCount) return false;
  if (state.pendingCount !== 0 || state.errorCount !== 0 || state.composerReady !== true) return false;
  if (state.sendControlPresent !== true || state.sendAvailable !== true) return false;
  if (state.progressPresent === true && state.progressCompleted !== true) return false;
  const expectedNames = normalizeAttachmentBasenameList(expectedBasenames);
  const hasExpectedBasenames = Array.isArray(expectedBasenames) && expectedBasenames.length > 0;
  if (hasExpectedBasenames) {
    if (!Array.isArray(state.inputFileBasenames)) return false;
    if (expectedNames.length !== expectedBasenames.length) return false;
    if (!sameAttachmentBasenameMultiset(state.inputFileBasenames, expectedNames)) return false;
  }
  // The composer can virtualize or summarize attachment chips. Visible tile
  // names are therefore supplementary evidence: reject an explicitly visible
  // contradictory filename, but never require every file to have a mounted
  // chip at this moment.
  const contradictionNames = expectedNames.length > 0
    ? expectedNames
    : state.inputFileBasenames;
  if (hasVisibleAttachmentBasenameContradiction(state.attachmentTileRecords, contradictionNames)) return false;
  const contradictionSet = new Set(normalizeAttachmentBasenameList(contradictionNames));
  if (normalizeAttachmentBasenameList(state.matchedBasenames).some((name) => !contradictionSet.has(name))) return false;
  return true;
}

export function isComposerSendable(state) {
  return state?.sendControlPresent === true && state?.sendAvailable === true;
}

export async function waitForAttachmentsReady(
  page,
  expectedCount,
  {
    expectedBasenames = [],
    timeoutMs = 30_000,
    pollMs = 250,
    readState = () => readAttachmentUploadState(page, { expectedBasenames }),
    sleep = (ms) => page.waitForTimeout(ms),
    diagnosticContext = {},
  } = {},
) {
  if (!Number.isInteger(expectedCount) || expectedCount <= 0) {
    throw new Error('Attachment readiness requires at least one expected file.');
  }
  const startedAt = Date.now();
  const deadline = Date.now() + timeoutMs;
  let lastState = null;
  let lastDiagnostics = attachmentReadinessDiagnostics(
    null,
    expectedCount,
    expectedBasenames,
    { ...diagnosticContext, elapsedMs: 0, finalPredicate: false },
  );
  while (Date.now() < deadline) {
    try {
      lastState = await readState();
    } catch (error) {
      error.diagnostics = attachmentReadinessDiagnostics(
        null,
        expectedCount,
        expectedBasenames,
        { ...diagnosticContext, elapsedMs: Date.now() - startedAt, finalPredicate: false },
      );
      throw error;
    }
    lastDiagnostics = attachmentReadinessDiagnostics(
      lastState,
      expectedCount,
      expectedBasenames,
      { ...diagnosticContext, elapsedMs: Date.now() - startedAt, finalPredicate: false },
    );
    if (lastState?.readReliable !== true) {
      const error = new Error('ChatGPT attachment DOM state was unreliable.');
      error.diagnostics = lastDiagnostics;
      throw error;
    }
    if (lastState.errorCount > 0) {
      const error = new Error('ChatGPT reported an attachment upload error.');
      error.diagnostics = lastDiagnostics;
      throw error;
    }
    const finalPredicate = isAttachmentUploadReady(lastState, expectedCount, expectedBasenames);
    if (finalPredicate) {
      lastDiagnostics = attachmentReadinessDiagnostics(
        lastState,
        expectedCount,
        expectedBasenames,
        { ...diagnosticContext, elapsedMs: Date.now() - startedAt, finalPredicate: true },
      );
      lastState.attachmentDiagnostics = lastDiagnostics;
      return lastState;
    }
    await sleep(Math.min(pollMs, Math.max(0, deadline - Date.now())));
  }
  lastDiagnostics = attachmentReadinessDiagnostics(
    lastState,
    expectedCount,
    expectedBasenames,
    { ...diagnosticContext, elapsedMs: Date.now() - startedAt, finalPredicate: false },
  );
  const error = new Error(`Timed out waiting for ChatGPT attachments to become ready. Diagnostics: ${JSON.stringify(lastDiagnostics)}`);
  error.diagnostics = lastDiagnostics;
  throw error;
}

export async function setAttachmentFiles(page, filePaths) {
  if (!Array.isArray(filePaths) || filePaths.length === 0) {
    throw new Error('At least one attachment path is required.');
  }
  const input = await findAttachmentFileInput(page);
  if (input) {
    await input.setInputFiles(filePaths);
    return { method: 'setInputFiles' };
  }

  const button = await findAttachmentButton(page);
  if (!button) throw new Error('Could not find a ChatGPT attachment control.');
  const chooserPromise = page.waitForEvent('filechooser');
  await button.click();
  const chooser = await chooserPromise;
  await chooser.setFiles(filePaths);
  return { method: 'filechooser.setFiles' };
}

export async function clearAttachmentFiles(page) {
  const errors = [];
  try {
    const input = await findAttachmentFileInput(page);
    if (input) await input.setInputFiles([]);
  } catch (error) {
    errors.push(error);
  }
  try {
    const removeButtons = page.locator(ATTACHMENT_REMOVE_SELECTOR);
    const count = await removeButtons.count();
    for (let index = count - 1; index >= 0; index -= 1) {
      const button = removeButtons.nth(index);
      if (await button.isVisible()) await button.click({ force: true });
    }
  } catch (error) {
    errors.push(error);
  }
  // Let the framework commit the input/removal event before checking that no
  // composer tile survived. This is only a cleanup turn, never a readiness
  // decision based on a fixed delay.
  try {
    if (typeof page.waitForTimeout === 'function') await page.waitForTimeout(0);
  } catch (error) {
    errors.push(error);
  }
  try {
    const remainingTiles = await countVisibleStrict(page.locator(ATTACHMENT_TILE_SELECTOR));
    if (remainingTiles > 0) {
      errors.push(new Error('ChatGPT attachment cleanup left a visible composer tile.'));
    }
  } catch (error) {
    errors.push(error);
  }
  try {
    const input = await findAttachmentFileInput(page);
    if (input) {
      const remainingFiles = await input.evaluate((element) => element.files?.length || 0);
      if (remainingFiles > 0) {
        errors.push(new Error('ChatGPT attachment cleanup left files selected in the input.'));
      }
    }
  } catch (error) {
    errors.push(error);
  }
  if (errors.length > 0) throw errors[0];
}

const ATTACHMENT_READINESS_TIMEOUT_PREFIX = 'Timed out waiting for ChatGPT attachments to become ready.';

function isAttachmentReadinessTimeout(error) {
  return typeof error?.message === 'string'
    && error.message.startsWith(ATTACHMENT_READINESS_TIMEOUT_PREFIX);
}

async function waitForAttachmentRetrySettle(page, delayMs, sleep) {
  if (!Number.isFinite(delayMs) || delayMs <= 0) return;
  if (typeof sleep === 'function') {
    await sleep(delayMs);
    return;
  }
  if (typeof page.waitForTimeout === 'function') {
    await page.waitForTimeout(delayMs);
    return;
  }
  await new Promise((resolve) => setTimeout(resolve, delayMs));
}

export async function uploadAttachments(
  page,
  filePaths,
  {
    expectedBasenames = [],
    timeoutMs = 30_000,
    pollMs = 250,
    diagnosticContext = {},
    // These optional hooks keep the retry state machine deterministic in
    // fake-page tests. Production callers use the bounded defaults above.
    readState = undefined,
    sleep = undefined,
    reattachAfterMs = ATTACHMENT_REATTACH_AFTER_MS,
    reattachSettleMs = ATTACHMENT_REATTACH_SETTLE_MS,
  } = {},
) {
  // A failed prior invocation can leave an unsent composer tile in the
  // persistent profile.  Clear only the composer attachment controls before
  // adding this invocation's explicit files; never touch conversation history.
  // Cleanup is a hard precondition. If an old tile cannot be removed, a new
  // upload could be mistaken for it and the bridge must not send a prompt.
  await clearAttachmentFiles(page);
  await setAttachmentFiles(page, filePaths);

  const startedAt = Date.now();
  const boundedTimeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 30_000;
  const boundedReattachAfterMs = Number.isFinite(reattachAfterMs) && reattachAfterMs > 0
    ? Math.min(reattachAfterMs, boundedTimeoutMs)
    : Math.min(ATTACHMENT_REATTACH_AFTER_MS, boundedTimeoutMs);
  const retryDiagnostics = {
    reattach_attempted: false,
    reattach_succeeded: false,
    reattach_attempt_count: 0,
    reattach_elapsed_ms: 0,
    existing_settle_attempted: false,
    existing_settle_succeeded: false,
    existing_settle_elapsed_ms: 0,
  };
  const waitOptions = (budgetMs) => ({
    expectedBasenames,
    timeoutMs: budgetMs,
    pollMs,
    diagnosticContext,
    ...(typeof readState === 'function' ? { readState } : {}),
    ...(typeof sleep === 'function' ? { sleep } : {}),
  });

  try {
    return await waitForAttachmentsReady(page, filePaths.length, waitOptions(boundedReattachAfterMs));
  } catch (firstError) {
    // Only a pure readiness timeout is eligible for one reattach. Explicit
    // upload errors and unreliable DOM reads are evidence of a different
    // failure and must remain fail-closed without another file operation.
    const remainingBeforeRetry = boundedTimeoutMs - (Date.now() - startedAt);
    if (
      !isAttachmentReadinessTimeout(firstError)
      || MAX_ATTACHMENT_REATTACH_ATTEMPTS < 1
      || remainingBeforeRetry <= 0
      || boundedReattachAfterMs >= boundedTimeoutMs
    ) {
      firstError.diagnostics = {
        ...(firstError.diagnostics || {}),
        ...retryDiagnostics,
      };
      throw firstError;
    }

    retryDiagnostics.reattach_attempted = true;
    retryDiagnostics.reattach_attempt_count = 1;
    const retryStartedAt = Date.now();
    try {
      const settleMs = Math.min(
        Number.isFinite(reattachSettleMs) && reattachSettleMs >= 0
          ? reattachSettleMs
          : ATTACHMENT_REATTACH_SETTLE_MS,
        Math.max(0, remainingBeforeRetry),
      );
      await waitForAttachmentRetrySettle(page, settleMs, sleep);
      await clearAttachmentFiles(page);
      // setAttachmentFiles re-runs active composer/file-input discovery. This
      // matters when the Project SPA replaced the original input during
      // hydration and the first FileList was accepted by a stale node.
      await setAttachmentFiles(page, filePaths);
      retryDiagnostics.reattach_succeeded = true;
    } catch (retryError) {
      retryDiagnostics.reattach_elapsed_ms = Date.now() - retryStartedAt;

      // A Project SPA can keep the original FileList and visible chips while
      // its uploader is still committing.  In that state cleanup may fail
      // because the chip is not removable yet.  Give the same pre-prompt
      // attachment set the remaining bounded budget to settle before
      // classifying the invocation as failed; do not select files again.
      const remainingAfterRetryFailure = boundedTimeoutMs - (Date.now() - startedAt);
      if (remainingAfterRetryFailure > 0) {
        retryDiagnostics.existing_settle_attempted = true;
        const settleStartedAt = Date.now();
        try {
          const state = await waitForAttachmentsReady(
            page,
            filePaths.length,
            waitOptions(remainingAfterRetryFailure),
          );
          retryDiagnostics.existing_settle_succeeded = true;
          retryDiagnostics.existing_settle_elapsed_ms = Date.now() - settleStartedAt;
          if (state && typeof state === 'object') {
            state.attachmentDiagnostics = {
              ...(state.attachmentDiagnostics || {}),
              ...retryDiagnostics,
            };
          }
          return state;
        } catch (settleError) {
          retryDiagnostics.existing_settle_elapsed_ms = Date.now() - settleStartedAt;
          retryError.diagnostics = {
            ...(firstError.diagnostics || {}),
            ...(retryError.diagnostics || {}),
            ...(settleError.diagnostics || {}),
            ...retryDiagnostics,
          };
          throw retryError;
        }
      }
      retryError.diagnostics = {
        ...(firstError.diagnostics || {}),
        ...(retryError.diagnostics || {}),
        ...retryDiagnostics,
      };
      throw retryError;
    }

    retryDiagnostics.reattach_elapsed_ms = Date.now() - retryStartedAt;
    const remainingAfterRetry = boundedTimeoutMs - (Date.now() - startedAt);
    if (remainingAfterRetry <= 0) {
      firstError.diagnostics = {
        ...(firstError.diagnostics || {}),
        ...retryDiagnostics,
      };
      throw firstError;
    }

    try {
      const state = await waitForAttachmentsReady(
        page,
        filePaths.length,
        waitOptions(remainingAfterRetry),
      );
      if (state && typeof state === 'object') {
        state.attachmentDiagnostics = {
          ...(state.attachmentDiagnostics || {}),
          ...retryDiagnostics,
        };
      }
      return state;
    } catch (secondError) {
      secondError.diagnostics = {
        ...(firstError.diagnostics || {}),
        ...(secondError.diagnostics || {}),
        ...retryDiagnostics,
      };
      throw secondError;
    }
  }
}

export async function createFreshConversation(
  page,
  {
    settleMs = DEFAULT_NEW_CHAT_ROUTE_TIMEOUT_MS,
    timeoutMs = undefined,
    pollMs = NEW_CHAT_ROUTE_POLL_MS,
    // A Project landing page exposes a "New chat" link that navigates to the
    // global root route.  Following that link would silently drop the
    // Project binding before the first prompt.  The caller can therefore
    // preserve the verified Project landing and let the first prompt create
    // the conversation route in place.
    preserveProjectScope = false,
  } = {},
) {
  const beforeUrl = page.url();
  if (preserveProjectScope) return {
    clicked: false,
    deferred: true,
    beforeUrl,
    afterUrl: beforeUrl,
    conversationId: null,
  };
  const control = await firstInteractive(page, NEW_CHAT_SELECTORS);
  if (!control) return {
    clicked: false,
    beforeUrl,
    afterUrl: beforeUrl,
    conversationId: null,
  };
  try {
    await control.click();
    const route = await waitForFreshConversationRoute(page, beforeUrl, {
      timeoutMs: timeoutMs === undefined ? settleMs : timeoutMs,
      pollMs,
    });
    return { clicked: true, beforeUrl, ...route };
  } catch {
    return { clicked: false, beforeUrl, afterUrl: page.url(), conversationId: null };
  }
}

export async function hasLoginIndicator(page) {
  if (isChatGptLoginUrl(page)) return true;
  return Boolean(await firstVisible(page, LOGIN_SELECTORS));
}

function normalizeAssistantRecord(record) {
  if (!record || record.visible !== true) return null;
  const id = typeof record.id === 'string' ? record.id.trim() : '';
  const slotId = (typeof record.slotId === 'string' ? record.slotId.trim() : '') || id;
  if (!id || !slotId) {
    throw new Error('Assistant messages do not expose unique stable identifiers.');
  }
  return {
    id,
    slotId,
    text: typeof record.text === 'string' ? record.text.trim() : String(record.text ?? '').trim(),
  };
}

/**
 * Reduce browser-side assistant candidates to one visible node per logical turn.
 *
 * This deliberately deduplicates by stable DOM identity, never by response text.
 * A visible slot with conflicting identities is ambiguous and therefore fails
 * closed instead of guessing which node is current.
 */
export function normalizeAssistantSnapshot(records) {
  if (!Array.isArray(records)) {
    throw new Error('Assistant snapshot records are invalid.');
  }
  const bySlot = new Map();
  const byId = new Map();
  const snapshot = [];
  const duplicateSlotIds = new Set();
  let visibleNodeCount = 0;
  for (const record of records) {
    if (record?.visible === true) visibleNodeCount += 1;
    const candidate = normalizeAssistantRecord(record);
    if (!candidate) {
      const hiddenSlot = typeof record?.slotId === 'string' ? record.slotId.trim() : '';
      if (hiddenSlot && bySlot.has(hiddenSlot)) duplicateSlotIds.add(hiddenSlot);
      continue;
    }

    const existingSlot = bySlot.get(candidate.slotId);
    if (existingSlot) {
      if (existingSlot.id !== candidate.id || existingSlot.text !== candidate.text) {
        throw new Error('Multiple visible assistant nodes conflict within one logical turn.');
      }
      duplicateSlotIds.add(candidate.slotId);
      continue;
    }
    const existingId = byId.get(candidate.id);
    if (existingId) {
      throw new Error('Assistant messages do not expose unique stable identifiers.');
    }
    bySlot.set(candidate.slotId, candidate);
    byId.set(candidate.id, candidate);
    snapshot.push(candidate);
  }
  const normalized = snapshot.map((item, index) => ({ index, ...item }));
  Object.defineProperty(normalized, 'duplicateSlotIds', {
    value: [...duplicateSlotIds],
    enumerable: false,
  });
  Object.defineProperty(normalized, 'visibleNodeCount', {
    value: visibleNodeCount,
    enumerable: false,
  });
  return normalized;
}

export async function readAssistantSnapshot(page) {
  const messages = page.locator(ASSISTANT_MESSAGE_SELECTOR);
  const records = await messages.evaluateAll((elements, { turnSelector, maxDepth }) => {
    function isVisible(element) {
      if (!element.isConnected) return false;
      if (typeof element.checkVisibility === 'function') {
        try {
          if (!element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
        } catch {
          if (!element.checkVisibility()) return false;
        }
      }
      for (let current = element; current; current = current.parentElement) {
        const style = getComputedStyle(current);
        if (
          current.hidden ||
          current.inert ||
          current.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' ||
          style.visibility === 'hidden' ||
          style.visibility === 'collapse' ||
          style.contentVisibility === 'hidden' ||
          Number(style.opacity) === 0
        ) {
          return false;
        }
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }

    function readAttribute(element, attribute) {
      const value = element?.getAttribute(attribute);
      return typeof value === 'string' ? value.trim() : '';
    }

    function readMessageId(element, turn) {
      let current = element;
      for (let depth = 0; current && depth < maxDepth; depth += 1, current = current.parentElement) {
        const messageId = readAttribute(current, 'data-message-id');
        if (messageId) return messageId;
        if (current === turn) break;
      }
      return readAttribute(turn, 'data-testid') || readAttribute(turn, 'id');
    }

    return elements.map((element, sourceIndex) => {
      const turn = element.closest(turnSelector);
      const slotId = readAttribute(turn, 'data-testid') || readAttribute(turn, 'id');
      return {
        sourceIndex,
        visible: isVisible(element),
        id: readMessageId(element, turn),
        slotId,
        text: element.innerText || '',
      };
    });
  }, { turnSelector: ASSISTANT_TURN_SELECTOR, maxDepth: MAX_STABLE_ANCESTOR_DEPTH });
  return normalizeAssistantSnapshot(records);
}

/** Return only the latest visible user-turn identity; never return prompt text. */
export async function readLatestVisibleUserTurnIdentity(page) {
  try {
    const records = await page.locator(USER_MESSAGE_SELECTOR).evaluateAll((elements, { turnSelector }) => {
      function hash(value) {
        if (!value) return null;
        let first = 0x811c9dc5;
        let second = 0x9e3779b9;
        const text = String(value || '');
        for (let index = 0; index < text.length; index += 1) {
          const code = text.charCodeAt(index);
          first = Math.imul(first ^ code, 0x01000193);
          second = Math.imul(second ^ (code + index), 0x85ebca6b);
        }
        const format = (number) => (number >>> 0).toString(16).padStart(8, '0');
        return `${format(first)}${format(second)}`;
      }
      function visible(element) {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return element.isConnected
          && !element.hidden
          && element.getAttribute('aria-hidden') !== 'true'
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && Number(style.opacity) !== 0
          && rect.width > 0
          && rect.height > 0;
      }
      function attr(element, name) {
        const value = element?.getAttribute(name);
        return typeof value === 'string' ? value.trim() : '';
      }
      return elements.map((element) => {
        const turn = element.closest(turnSelector);
        const id = attr(element, 'data-message-id') || attr(turn, 'data-message-id') || attr(turn, 'data-testid') || attr(turn, 'id');
        const slot = attr(turn, 'data-testid') || attr(turn, 'id') || id;
        const text = String(element.innerText || element.textContent || '');
        return { visible: visible(element), idHash: hash(id), slotHash: hash(slot), textHash: hash(text), textLength: text.length };
      }).filter((record) => record.visible);
    }, { turnSelector: ASSISTANT_TURN_SELECTOR });
    return records.at(-1) || null;
  } catch {
    return null;
  }
}

export function validateStableAssistantSnapshot(snapshot) {
  if (!Array.isArray(snapshot)) {
    throw new Error('Assistant snapshot is invalid.');
  }
  const ids = snapshot.map((item) => item?.id);
  if (ids.some((id) => typeof id !== 'string' || !id.trim()) || new Set(ids).size !== ids.length) {
    throw new Error('Assistant messages do not expose unique stable identifiers.');
  }
  const slots = snapshot.map((item) => item?.slotId).filter((slotId) => slotId !== undefined);
  if (slots.length > 0 && (slots.length !== snapshot.length || slots.some((slotId) => typeof slotId !== 'string' || !slotId.trim()) || new Set(slots).size !== slots.length)) {
    throw new Error('Assistant messages do not expose unique logical turn identifiers.');
  }
  return ids;
}

export function extractNewAssistantResponse(snapshot, baselineIds) {
  const baseline = new Set(baselineIds);
  const newMessages = snapshot.filter((item) => !baseline.has(item.id));
  if (newMessages.length === 0) return null;
  if (newMessages.length !== 1 || !newMessages[0].id) {
    throw new Error('Expected exactly one new assistant message with a stable identifier.');
  }
  return newMessages[0];
}

export async function waitForStableAssistantSnapshot(
  readSnapshot,
  {
    requireNonEmpty = false,
    timeoutMs = 15_000,
    pollMs = 250,
    stabilityMs = 1_500,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  } = {},
) {
  const effectiveTimeoutMs = normalizeAssistantResponseTimeoutMs(timeoutMs);
  const deadline = Date.now() + effectiveTimeoutMs;
  let previousSignature = null;
  let stableSince = 0;

  while (Date.now() < deadline) {
    const snapshot = await readSnapshot();
    validateStableAssistantSnapshot(snapshot);
    const signature = snapshot
      .map((item) => `${item.index}:${item.slotId || ''}:${item.id}:${item.text.length}`)
      .join('\u0000');
    if (signature !== previousSignature) {
      previousSignature = signature;
      stableSince = Date.now();
    }
    if (
      (!requireNonEmpty || snapshot.length > 0)
      && Date.now() - stableSince >= stabilityMs
    ) {
      return snapshot;
    }
    await sleep(pollMs);
  }

  throw new Error('Timed out waiting for a stable assistant snapshot.');
}

export async function isGenerating(page) {
  if (await firstVisible(page, STOP_SELECTORS)) return true;
  const busy = await page.locator('[aria-busy="true"]').count();
  const streaming = await page.locator('[data-is-streaming="true"], [data-state="streaming"]').count();
  return busy > 0 || streaming > 0;
}

/**
 * Read only bounded, boolean response-state indicators.  This deliberately
 * never returns banner text or HTML: the values are used only to classify a
 * failed response and are safe to persist in a receipt.
 */
export async function readResponseUiState(page) {
  const state = {
    rateLimitBannerDetected: false,
    tooManyRequestsDetected: false,
    genericErrorBannerDetected: false,
    retryButtonDetected: false,
    stopButtonPresent: false,
    reliable: true,
  };
  try {
    const banners = await page.locator(RESPONSE_BANNER_SELECTOR).evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (
        element.hidden
        || element.getAttribute('aria-hidden') === 'true'
        || style.display === 'none'
        || style.visibility === 'hidden'
        || Number(style.opacity) === 0
        || rect.width <= 0
        || rect.height <= 0
      ) return null;
      const text = String(element.innerText || element.textContent || '').toLowerCase();
      return {
        rateLimit: /rate\s*limit|limit reached|usage limit|quota/i.test(text),
        tooManyRequests: /too many requests|429|try again later/i.test(text),
        genericError: /error|failed|unable|problem|network/i.test(text),
      };
    }).filter(Boolean));
    state.rateLimitBannerDetected = banners.some((item) => item.rateLimit);
    state.tooManyRequestsDetected = banners.some((item) => item.tooManyRequests);
    state.genericErrorBannerDetected = banners.some((item) => item.genericError);
  } catch {
    state.reliable = false;
  }
  try {
    state.retryButtonDetected = await page.locator(RESPONSE_RETRY_BUTTON_SELECTOR).evaluateAll((elements) => elements.some((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (
        element.hidden
        || element.getAttribute('aria-hidden') === 'true'
        || style.display === 'none'
        || style.visibility === 'hidden'
        || Number(style.opacity) === 0
        || rect.width <= 0
        || rect.height <= 0
      ) return false;
      const label = `${element.getAttribute('aria-label') || ''} ${element.innerText || ''}`.toLowerCase();
      return /retry|try again|重试/i.test(label);
    }));
  } catch {
    state.reliable = false;
  }
  try {
    state.stopButtonPresent = Boolean(await firstVisible(page, STOP_SELECTORS));
  } catch {
    state.reliable = false;
  }
  return state;
}

function forensicCandidateSummary(snapshot, baselineIds, generating, completion) {
  const baseline = new Set(Array.isArray(baselineIds) ? baselineIds : []);
  return snapshotDiagnosticRecords(snapshot)
    .slice(0, MAX_RESPONSE_DIAGNOSTIC_CANDIDATES)
    .map((item) => ({
      logical_slot_hash: diagnosticHash(item.slotId || item.id),
      message_id_hash: diagnosticHash(item.id),
      visible: true,
      is_new: typeof item.id === 'string' && !baseline.has(item.id),
      text_length: safeDiagnosticInteger(typeof item.text === 'string' ? item.text.length : null),
      text_sha256: typeof item.text === 'string' ? sha256DiagnosticValue(item.text) : null,
      streaming: generating === null ? null : Boolean(generating),
      completion: completion === null ? null : Boolean(completion),
    }));
}

function classifyResponseFailure(error, {
  candidateSeen,
  candidateNonEmpty,
  generating,
  uiState,
  rejectInvariant,
} = {}) {
  if (uiState?.rateLimitBannerDetected || uiState?.tooManyRequestsDetected) return RESPONSE_FAILURE_CLASSES.CHATGPT_UI_RATE_LIMIT;
  if (uiState?.genericErrorBannerDetected || (uiState?.retryButtonDetected && !candidateSeen)) return RESPONSE_FAILURE_CLASSES.CHATGPT_UI_ERROR;
  if (rejectInvariant === 'snapshot_identity_conflict' || rejectInvariant === 'multiple_new_assistant_slots') {
    return RESPONSE_FAILURE_CLASSES.ASSISTANT_IDENTITY_HYDRATION_CONFLICT;
  }
  if (rejectInvariant === 'generation_state_read_failed') return RESPONSE_FAILURE_CLASSES.UNKNOWN_RESPONSE_FAILURE;
  if (rejectInvariant === 'response_snapshot_or_extractor_error') return RESPONSE_FAILURE_CLASSES.UNKNOWN_RESPONSE_FAILURE;
  if (error?.message?.startsWith('Timed out')) {
    return candidateSeen
      ? RESPONSE_FAILURE_CLASSES.ASSISTANT_TURN_APPEARED_NOT_COMPLETED
      : RESPONSE_FAILURE_CLASSES.NO_NEW_ASSISTANT_TURN;
  }
  if (candidateSeen && candidateNonEmpty) return RESPONSE_FAILURE_CLASSES.ASSISTANT_RESPONSE_VISIBLE_EXTRACTOR_REJECTED;
  return RESPONSE_FAILURE_CLASSES.UNKNOWN_RESPONSE_FAILURE;
}

export async function waitForStableAssistant(
  readSnapshot,
  readGenerating,
  baselineIds,
  {
    baselineCount = 0,
    baselineSnapshot = undefined,
    readUiState = undefined,
    readUserTurn = undefined,
    startedAt = Date.now(),
    sendStartedAt = undefined,
    sendCompletedAt = undefined,
    timeoutMs = DEFAULT_ASSISTANT_RESPONSE_TIMEOUT_MS,
    pollMs = 500,
    stabilityMs = 1_500,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    refresh = undefined,
    refreshDelayMs = EMPTY_ASSISTANT_REFRESH_DELAY_MS,
    activeRefreshDelayMs = ACTIVE_EMPTY_ASSISTANT_REFRESH_DELAY_MS,
  } = {},
) {
  const effectiveTimeoutMs = normalizeAssistantResponseTimeoutMs(timeoutMs);
  const numericRefreshDelay = Number(refreshDelayMs);
  const effectiveRefreshDelayMs = Number.isFinite(numericRefreshDelay) && numericRefreshDelay >= 0
    ? Math.min(MAX_EMPTY_ASSISTANT_REFRESH_DELAY_MS, numericRefreshDelay)
    : EMPTY_ASSISTANT_REFRESH_DELAY_MS;
  const numericActiveRefreshDelay = Number(activeRefreshDelayMs);
  const effectiveActiveRefreshDelayMs = Number.isFinite(numericActiveRefreshDelay) && numericActiveRefreshDelay >= 0
    ? Math.min(MAX_ACTIVE_EMPTY_ASSISTANT_REFRESH_DELAY_MS, numericActiveRefreshDelay)
    : ACTIVE_EMPTY_ASSISTANT_REFRESH_DELAY_MS;
  const deadline = Date.now() + effectiveTimeoutMs;
  const timelineStartedAt = Number.isFinite(startedAt) ? startedAt : Date.now();
  const baselineSummary = snapshotDiagnosticSummary(baselineSnapshot, baselineIds);
  const baselineIdHashes = baselineSummary.idHashes.length > 0
    ? baselineSummary.idHashes
    : boundedDiagnosticList(baselineIds, (id) => diagnosticHash(id));
  const baselineSlotHashes = baselineSummary.slotHashes.length > 0
    ? baselineSummary.slotHashes
    : baselineIdHashes;
  const forensic = {
    baseline_assistant_count: baselineSummary.snapshotCount || safeDiagnosticInteger(baselineCount, 0),
    baseline_assistant_id_hashes: baselineIdHashes,
    baseline_logical_slot_hashes: baselineSlotHashes,
    latest_user_turn_id_hash: null,
    latest_user_turn_slot_hash: null,
    latest_user_turn_text_hash: null,
    latest_user_turn_text_length: null,
    submitted_user_turn_id_hash: null,
    submitted_user_turn_slot_hash: null,
    submitted_user_turn_text_hash: null,
    submitted_user_turn_text_length: null,
    user_turn_submitted: false,
    visible_assistant_count: baselineSummary.snapshotCount,
    visible_assistant_dom_node_count: baselineSummary.visibleNodeCount,
    unique_logical_slot_count: baselineSummary.slotHashes.length,
    new_assistant_count: 0,
    candidates: [],
    duplicate_logical_slot_hashes: [],
    rate_limit_banner_detected: false,
    too_many_requests_detected: false,
    generic_error_banner_detected: false,
    retry_button_detected: false,
    generation_stopped_detected: false,
    stop_button_present: null,
    generation_active: null,
    streaming_seen: false,
    completion_seen: false,
    completion_predicate: false,
    t_send_started_ms: Number.isFinite(sendStartedAt) ? Math.max(0, sendStartedAt - timelineStartedAt) : null,
    t_send_completed_ms: Number.isFinite(sendCompletedAt) ? Math.max(0, sendCompletedAt - timelineStartedAt) : null,
    t_generation_indicator_first_seen_ms: null,
    t_first_new_visible_assistant_slot_ms: null,
    t_first_nonempty_assistant_text_ms: null,
    t_generation_indicator_gone_ms: null,
    t_response_text_stable_ms: null,
    t_completion_predicate_true_ms: null,
    t_extractor_success_ms: null,
    t_main_deadline_expired_ms: null,
    empty_assistant_refresh_attempted: false,
    empty_assistant_refresh_succeeded: false,
    t_empty_assistant_refresh_ms: null,
    empty_assistant_refresh_trigger: null,
    first_new_message_id_hash: null,
    latest_message_id_hash: null,
    latest_text_length: 0,
    latest_text_sha256: null,
    elapsed_ms: 0,
    deadline_ms: effectiveTimeoutMs,
    observation_count: 0,
    response_failure_class: null,
    reject_invariant: null,
  };
  let candidateSlot = null;
  let previousText = null;
  let stableSince = 0;
  let sawContentChange = false;
  let candidateSeen = false;
  let candidateNonEmpty = false;
  let latestGenerating = null;
  let latestUiState = null;
  let stopObserved = false;
  let emptyCandidateSince = null;
  let refreshAttempted = false;

  const elapsed = () => Math.max(0, Date.now() - timelineStartedAt);
  const attachForensic = (error, responseFailureClass, rejectInvariant = null) => {
    forensic.elapsed_ms = elapsed();
    forensic.response_failure_class = responseFailureClass;
    forensic.reject_invariant = rejectInvariant;
    error.responseForensic = forensic;
    return error;
  };

  while (Date.now() < deadline) {
    try {
      const snapshot = await readSnapshot();
      validateStableAssistantSnapshot(snapshot);
      forensic.observation_count += 1;
      const summary = snapshotDiagnosticSummary(snapshot, baselineIds);
      forensic.visible_assistant_count = summary.snapshotCount;
      forensic.visible_assistant_dom_node_count = summary.visibleNodeCount;
      forensic.unique_logical_slot_count = summary.slotHashes.length;
      forensic.new_assistant_count = summary.newCount;
      forensic.duplicate_logical_slot_hashes = summary.duplicateSlotHashes;
      forensic.latest_message_id_hash = summary.idHashes.at(-1) || null;
      const latest = snapshotDiagnosticRecords(snapshot).at(-1);
      forensic.latest_text_length = safeDiagnosticInteger(typeof latest?.text === 'string' ? latest.text.length : null, 0);
      forensic.latest_text_sha256 = typeof latest?.text === 'string' ? sha256DiagnosticValue(latest.text) : null;
      forensic.candidates = forensicCandidateSummary(snapshot, baselineIds, latestGenerating, forensic.completion_predicate);

      if (typeof readUiState === 'function') {
        try {
          latestUiState = await readUiState();
          if (latestUiState && typeof latestUiState === 'object') {
            forensic.rate_limit_banner_detected = Boolean(latestUiState.rateLimitBannerDetected);
            forensic.too_many_requests_detected = Boolean(latestUiState.tooManyRequestsDetected);
            forensic.generic_error_banner_detected = Boolean(latestUiState.genericErrorBannerDetected);
            forensic.retry_button_detected = Boolean(latestUiState.retryButtonDetected);
            forensic.stop_button_present = typeof latestUiState.stopButtonPresent === 'boolean'
              ? latestUiState.stopButtonPresent
              : forensic.stop_button_present;
            if (forensic.stop_button_present === true) stopObserved = true;
            if (stopObserved && forensic.stop_button_present === false) forensic.generation_stopped_detected = true;
          }
        } catch {
          latestUiState = null;
        }
      }
      if (typeof readUserTurn === 'function') {
        try {
          const userTurn = await readUserTurn();
          if (userTurn && typeof userTurn === 'object') {
            forensic.latest_user_turn_id_hash = typeof userTurn.idHash === 'string' ? userTurn.idHash : forensic.latest_user_turn_id_hash;
            forensic.latest_user_turn_slot_hash = typeof userTurn.slotHash === 'string' ? userTurn.slotHash : forensic.latest_user_turn_slot_hash;
            forensic.latest_user_turn_text_hash = typeof userTurn.textHash === 'string' ? userTurn.textHash : forensic.latest_user_turn_text_hash;
            forensic.latest_user_turn_text_length = Number.isInteger(userTurn.textLength) && userTurn.textLength >= 0
              ? userTurn.textLength
              : forensic.latest_user_turn_text_length;
            forensic.submitted_user_turn_id_hash = forensic.latest_user_turn_id_hash;
            forensic.submitted_user_turn_slot_hash = forensic.latest_user_turn_slot_hash;
            forensic.submitted_user_turn_text_hash = forensic.latest_user_turn_text_hash;
            forensic.submitted_user_turn_text_length = forensic.latest_user_turn_text_length;
            forensic.user_turn_submitted = Boolean(userTurn.visible);
          }
        } catch {
          // User-turn identity is best-effort forensic data and never changes
          // the response success predicate.
        }
      }

      let candidate = null;
      // A streaming turn can hydrate in place: the final assistant node may
      // replace a placeholder ID while the visible assistant-node count stays
      // unchanged.  Candidate discovery therefore follows stable identity
      // delta, not a count increase.  extractNewAssistantResponse remains
      // fail-closed when zero or multiple new identities are visible.
      try {
        candidate = extractNewAssistantResponse(snapshot, baselineIds);
      } catch (error) {
        const invariant = error?.message?.startsWith('More than one logical')
          ? 'multiple_new_assistant_slots'
          : error?.message?.includes('exactly one new assistant')
            ? 'multiple_new_assistant_messages'
            : 'snapshot_identity_conflict';
        throw attachForensic(error, RESPONSE_FAILURE_CLASSES.ASSISTANT_IDENTITY_HYDRATION_CONFLICT, invariant);
      }
      if (candidate) {
        candidateSeen = true;
        if (forensic.t_first_new_visible_assistant_slot_ms === null) {
          forensic.t_first_new_visible_assistant_slot_ms = elapsed();
          forensic.first_new_message_id_hash = diagnosticHash(candidate.id);
        }
        // ChatGPT may replace a streaming placeholder node (and its ID) with the
        // final node while keeping the same logical assistant slot. Accept that
        // replacement only when the slot is unchanged; a second slot is unsafe.
        const slot = typeof candidate.slotId === 'string' && candidate.slotId
          ? candidate.slotId
          : Number.isInteger(candidate.index) ? candidate.index : null;
        if (candidateSlot !== null && slot !== null && candidateSlot !== slot) {
          throw attachForensic(
            new Error('More than one logical new assistant slot appeared.'),
            RESPONSE_FAILURE_CLASSES.ASSISTANT_IDENTITY_HYDRATION_CONFLICT,
            'multiple_new_assistant_slots',
          );
        }
        if (candidateSlot === null) candidateSlot = slot;
        if (candidate.text !== previousText) {
          if (candidate.text) {
            sawContentChange = true;
            candidateNonEmpty = true;
            if (forensic.t_first_nonempty_assistant_text_ms === null) forensic.t_first_nonempty_assistant_text_ms = elapsed();
          }
          previousText = candidate.text;
          stableSince = Date.now();
        }
        try {
          latestGenerating = await readGenerating();
        } catch (error) {
          throw attachForensic(
            error,
            classifyResponseFailure(error, {
              candidateSeen,
              candidateNonEmpty,
              generating: latestGenerating,
              uiState: latestUiState,
              rejectInvariant: 'generation_state_read_failed',
            }),
            'generation_state_read_failed',
          );
        }
        forensic.generation_active = Boolean(latestGenerating);
        if (latestGenerating) {
          forensic.streaming_seen = true;
          if (forensic.t_generation_indicator_first_seen_ms === null) forensic.t_generation_indicator_first_seen_ms = elapsed();
        } else if (forensic.streaming_seen && forensic.t_generation_indicator_gone_ms === null) {
          forensic.t_generation_indicator_gone_ms = elapsed();
        }
        if (!candidate.text && typeof refresh === 'function') {
          if (emptyCandidateSince === null) emptyCandidateSince = Date.now();
          const refreshDelay = latestGenerating
            ? effectiveActiveRefreshDelayMs
            : effectiveRefreshDelayMs;
          if (!refreshAttempted && Date.now() - emptyCandidateSince >= refreshDelay) {
            refreshAttempted = true;
            forensic.empty_assistant_refresh_attempted = true;
            forensic.t_empty_assistant_refresh_ms = elapsed();
            forensic.empty_assistant_refresh_trigger = latestGenerating
              ? 'active_generation'
              : 'stopped_generation';
            try {
              const refreshed = await refresh();
              // A route-aware refresh can decline when the SPA has not yet
              // committed a conversation URL.  Treat that as a bounded
              // observation, not as a successful reload of the global root.
              if (refreshed === false) continue;
              forensic.empty_assistant_refresh_succeeded = true;
              // Reloading can replace both the placeholder message id and its
              // DOM slot. Start identity tracking again against the same
              // baseline; no second prompt is permitted or sent here.
              candidateSlot = null;
              previousText = null;
              stableSince = Date.now();
              sawContentChange = false;
              emptyCandidateSince = null;
              latestGenerating = null;
              continue;
            } catch {
              // Keep the original response failure classification and wait
              // for the hard deadline if the one recovery reload fails.
            }
          }
        } else if (candidate.text) {
          emptyCandidateSince = null;
        }
        const complete = Boolean(candidate.text && sawContentChange && !latestGenerating && Date.now() - stableSince >= stabilityMs);
        forensic.completion_predicate = complete;
        forensic.candidates = forensicCandidateSummary(snapshot, baselineIds, latestGenerating, complete);
        if (complete) {
          forensic.completion_seen = true;
          forensic.t_response_text_stable_ms = elapsed();
          forensic.t_completion_predicate_true_ms = elapsed();
          forensic.t_extractor_success_ms = elapsed();
          return candidate.text;
        }
      }
    } catch (error) {
      if (!error.responseForensic) {
        const invariant = /unique stable|unique logical|conflict|invalid snapshot/i.test(error?.message || '')
          ? 'snapshot_identity_conflict'
          : 'response_snapshot_or_extractor_error';
        const failureClass = classifyResponseFailure(error, {
          candidateSeen,
          candidateNonEmpty,
          generating: latestGenerating,
          uiState: latestUiState,
          rejectInvariant: invariant,
        });
        throw attachForensic(error, failureClass, invariant);
      }
      throw error;
    }
    await sleep(pollMs);
  }

  forensic.t_main_deadline_expired_ms = elapsed();
  forensic.elapsed_ms = forensic.t_main_deadline_expired_ms;
  const failureClass = classifyResponseFailure(
    new Error('Timed out waiting for a completed new assistant response.'),
    { candidateSeen, candidateNonEmpty, generating: latestGenerating, uiState: latestUiState },
  );
  forensic.response_failure_class = failureClass;
  forensic.reject_invariant = 'response_deadline';
  const timeoutError = new Error('Timed out waiting for a completed new assistant response.');
  timeoutError.responseForensic = forensic;
  throw timeoutError;
}

export async function waitForNewAssistantResponse(
  page,
  baselineIds,
  options = {},
) {
  const responseOptions = {
    ...options,
    readUiState: () => readResponseUiState(page),
    readUserTurn: () => readLatestVisibleUserTurnIdentity(page),
    sleep: (ms) => page.waitForTimeout(ms),
  };
  if (responseOptions.refresh === undefined && typeof page.reload === 'function') {
    responseOptions.refresh = async () => {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60_000 });
      if (typeof page.waitForTimeout === 'function') await page.waitForTimeout(750);
    };
  }
  return waitForStableAssistant(
    () => readAssistantSnapshot(page),
    () => isGenerating(page),
    baselineIds,
    responseOptions,
  );
}

export async function waitForComposerOrLogin(page, { timeoutMs = 10_000, pollMs = 250 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await hasLoginIndicator(page)) return { composer: null, loginRequired: true };
    const composer = await findComposer(page);
    if (composer) return { composer, loginRequired: false };
    await page.waitForTimeout(pollMs);
  }
  const loginRequired = await hasLoginIndicator(page);
  return { composer: loginRequired ? null : await findComposer(page), loginRequired };
}
