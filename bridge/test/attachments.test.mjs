import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import {
  ATTACHMENT_ROOTS_ENV,
  BRIDGE_ROOT,
  BridgeError,
  buildReceipt,
  consultOnce,
  assertAssistantBaselineUnchanged,
  FAILURE_CODES,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENTS,
  prepareAttachments,
  resolveAllowedAttachmentRoots,
  validateAttachmentPath,
  validateReceiptAttachments,
} from '../src/bridge.mjs';
import {
  attachmentReadinessDiagnostics,
  findAttachmentFileInput,
  isAttachmentUploadReady,
  isComposerSendable,
  readAttachmentFileInputState,
  readAttachmentUploadState,
  uploadAttachments,
  waitForAttachmentsReady,
} from '../src/chatgpt-ui.mjs';
import {
  consultGptInputSchema,
  createConsultGptHandler,
} from '../src/mcp-server.mjs';

const FIXTURE = path.join(BRIDGE_ROOT, 'test', 'fixtures', 'attachment_nonce.txt');
const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';

async function removeTestRoot(rootDir) {
  await fs.rm(rootDir, { recursive: true, force: true });
}

function fakeBridge({ uploadError = null } = {}) {
  const events = [];
  const bridge = {
    requestCount: 0,
    async open() { events.push('open'); },
    async navigate() { events.push('navigate'); return 'https://chatgpt.com/'; },
    async ensureLoggedIn() { events.push('login'); },
    async createFreshConversation() {
      events.push('fresh');
      return { clicked: true, beforeUrl: 'https://chatgpt.com/', afterUrl: `https://chatgpt.com/c/${CONVERSATION_ID}` };
    },
    async waitForAssistantBaseline() {
      events.push('baseline');
      return [];
    },
    async uploadPreparedAttachments(files) {
      events.push(`upload:${files.length}`);
      if (uploadError) throw uploadError;
    },
    async sendOnePrompt(prompt) {
      events.push(`send:${prompt}`);
      bridge.requestCount += 1;
      return 'FIXTURE_ATTACHMENT_NONCE';
    },
    currentUrl() { return `https://chatgpt.com/c/${CONVERSATION_ID}`; },
    async close() { events.push('close'); },
    releaseForManualLogin() { events.push('release'); },
  };
  return { bridge, events };
}

function fakeAttachmentPage({ clearError = null, visibleTiles = 0, removeCount = 0, removeError = null } = {}) {
  const calls = [];
  let selectedFileCount = 1;
  const input = {
    async setInputFiles(files) {
      calls.push({ type: 'setInputFiles', files });
      if (Array.isArray(files) && files.length === 0) {
        if (clearError) throw clearError;
        selectedFileCount = 0;
      }
    },
    async evaluate() {
      return selectedFileCount;
    },
  };
  const removeButton = {
    async isVisible() { return true; },
    async click() {
      calls.push({ type: 'remove' });
      if (removeError) throw removeError;
    },
  };
  const locatorFor = (selector) => {
    if (selector === 'input[type="file"]') {
      return {
        async count() { return 1; },
        nth() { return input; },
        first() { return input; },
      };
    }
    if (selector.includes('button[aria-label')) {
      return {
        async count() { return removeCount; },
        nth() { return removeButton; },
      };
    }
    return {
      async count() { return visibleTiles; },
      nth() { return { async isVisible() { return true; } }; },
    };
  };
  return {
    calls,
    locator: locatorFor,
    async waitForTimeout() {},
  };
}

function fakeFileInputSelectionPage(inputs) {
  const locatorFor = (values) => ({
    async count() { return values.length; },
    nth(index) { return values[index]; },
    first() { return values[0] || null; },
  });
  return {
    locator(selector) {
      if (selector === 'input[type="file"]') return locatorFor(inputs);
      if (selector === 'form[data-type="unified-composer"] input[type="file"]') {
        return locatorFor(inputs.filter((input) => input.meta.owner === 'unified-composer'));
      }
      return locatorFor([]);
    },
  };
}

function fakeAttachmentUploadStatePage({
  fileNames,
  tileBasenames = [],
  tileRecords = null,
  pendingCount = 0,
  errorCount = 0,
  sendAvailable = true,
  progressRecords = [],
} = {}) {
  const input = {
    async evaluate(callback) {
      if (String(callback).includes('files?.length')) {
        return { count: fileNames.length, basenames: fileNames };
      }
      return {
        owner: 'unified-composer',
        active: true,
        accept: '',
        multiple: true,
      };
    },
  };
  const inputLocator = {
    async count() { return 1; },
    nth() { return input; },
    first() { return input; },
    async evaluate(callback) { return input.evaluate(callback); },
  };
  const visibleCountLocator = (count) => ({
    async count() { return count; },
    nth() {
      return {
        async isVisible() { return true; },
      };
    },
  });
  const composer = {
    async isVisible() { return true; },
    async isEnabled() { return true; },
    async evaluate() { return { disabled: false, ariaDisabled: false }; },
  };
  const send = {
    async isVisible() { return true; },
    async isEnabled() { return sendAvailable; },
    async evaluate() { return { disabled: !sendAvailable, ariaDisabled: false }; },
  };
  const oneVisible = (locator) => ({
    async count() { return 1; },
    nth() { return locator; },
    first() { return locator; },
  });
  const tileLocator = {
    async evaluateAll() {
      return Array.isArray(tileRecords)
        ? tileRecords
        : tileBasenames.map((basename) => ({ visible: true, candidates: [basename] }));
    },
  };
  const progressLocator = {
    async evaluateAll() { return progressRecords; },
  };
  const emptyLocator = visibleCountLocator(0);
  return {
    locator(selector) {
      if (selector === 'input[type="file"]' || selector === 'form[data-type="unified-composer"] input[type="file"]') {
        return inputLocator;
      }
      if (
        selector.includes('cursor-wait')
        || selector.includes('aria-busy')
        || selector.includes('data-state="loading"')
        || selector.includes('data-status="uploading"')
        || selector.includes('aria-label*="uploading"')
        || selector.includes('aria-label*="上传中"')
      ) {
        return visibleCountLocator(pendingCount);
      }
      if (
        selector.includes('data-state="error"')
        || selector.includes('data-status="error"')
        || selector.includes('upload failed')
        || selector.includes('attachment error')
        || selector.includes('上传失败')
      ) {
        return visibleCountLocator(errorCount);
      }
      if (
        selector.includes('[role="progressbar"]')
        || selector.includes('progress')
        || selector.includes('aria-valuenow')
        || selector.includes('data-progress')
        || selector.includes('data-upload-progress')
      ) {
        return progressLocator;
      }
      if (
        selector.includes('[role="group"][aria-label]')
        || selector.includes('[data-attachment-id]')
        || selector.includes('[data-file-id]')
        || selector.includes('[role="listitem"]')
      ) {
        return tileLocator;
      }
      return emptyLocator;
    },
    getByPlaceholder() {
      return oneVisible(composer);
    },
    getByRole(role) {
      return oneVisible(role === 'button' ? send : composer);
    },
  };
}

function fakeReattachPage() {
  const events = [];
  let selectedFiles = [];
  let selectionCount = 0;
  const input = {
    async setInputFiles(files) {
      const nextFiles = Array.isArray(files) ? [...files] : [];
      events.push({ type: 'setInputFiles', files: nextFiles });
      selectedFiles = nextFiles;
      if (nextFiles.length > 0) selectionCount += 1;
    },
    async evaluate(callback) {
      if (String(callback).includes('files?.length')) {
        return {
          count: selectedFiles.length,
          basenames: selectedFiles.map((file) => path.basename(file)),
        };
      }
      return {
        owner: 'unified-composer',
        active: true,
        accept: '',
        multiple: true,
      };
    },
  };
  const inputLocator = {
    async count() { return 1; },
    nth() { return input; },
    first() { return input; },
  };
  const emptyLocator = {
    async count() { return 0; },
    nth() { return null; },
    first() { return null; },
  };
  return {
    events,
    get selectionCount() { return selectionCount; },
    locator(selector) {
      if (selector === 'input[type="file"]' || selector === 'form[data-type="unified-composer"] input[type="file"]') {
        return inputLocator;
      }
      return emptyLocator;
    },
    async waitForTimeout() {},
  };
}

function fakeRetryCleanupSettlingPage() {
  let selectedFiles = [];
  let clearCount = 0;
  let uploadCount = 0;
  let phase = 'initial';
  const input = {
    async setInputFiles(files) {
      const nextFiles = Array.isArray(files) ? [...files] : [];
      if (nextFiles.length === 0) {
        clearCount += 1;
        if (clearCount >= 2) {
          phase = 'settled';
          throw new Error('attachment chip is still committing');
        }
        selectedFiles = [];
        return;
      }
      uploadCount += 1;
      selectedFiles = nextFiles;
    },
    async evaluate(callback) {
      if (String(callback).includes('files?.length')) {
        return {
          count: selectedFiles.length,
          basenames: selectedFiles.map((file) => path.basename(file)),
        };
      }
      return {
        owner: 'unified-composer',
        active: true,
        accept: '',
        multiple: true,
      };
    },
  };
  const inputLocator = {
    async count() { return 1; },
    nth() { return input; },
    first() { return input; },
  };
  const emptyLocator = {
    async count() { return 0; },
    nth() { return null; },
    first() { return null; },
  };
  return {
    get uploadCount() { return uploadCount; },
    get phase() { return phase; },
    locator(selector) {
      if (selector === 'input[type="file"]' || selector === 'form[data-type="unified-composer"] input[type="file"]') {
        return inputLocator;
      }
      return emptyLocator;
    },
    async waitForTimeout() {},
  };
}

function fakeFileInput(meta, selectedFileCount = 0) {
  const input = {
    meta,
    async setInputFiles(files) {
      selectedFileCount = Array.isArray(files) ? files.length : 0;
    },
    async evaluate(callback) {
      return String(callback).includes('files?.length') ? selectedFileCount : meta;
    },
  };
  return input;
}

test('file input selection prefers active unified composer and exposes bounded metadata', async () => {
  const staleGlobalInput = fakeFileInput({
    owner: 'global-fallback',
    active: false,
    accept: '.txt',
    multiple: false,
  });
  const activeComposerInput = fakeFileInput({
    owner: 'unified-composer',
    active: true,
    accept: 'text/plain,image/png',
    multiple: true,
  });
  const page = fakeFileInputSelectionPage([staleGlobalInput, activeComposerInput]);

  assert.equal(await findAttachmentFileInput(page), activeComposerInput);
  assert.deepEqual(await readAttachmentFileInputState(page), {
    locator: activeComposerInput,
    totalCount: 2,
    selectedIndex: 1,
    owner: 'unified-composer',
    accept: 'text/plain,image/png',
    multiple: true,
    composerScopedCount: 1,
    reliable: true,
  });
});

test('file input selection uses global fallback only when no composer input exists', async () => {
  const fallbackInput = fakeFileInput({
    owner: 'global-fallback',
    active: false,
    accept: '.txt',
    multiple: false,
  });
  const page = fakeFileInputSelectionPage([fallbackInput]);
  const state = await readAttachmentFileInputState(page);
  assert.equal(state.locator, fallbackInput);
  assert.equal(state.owner, 'global-fallback');
  assert.equal(state.selectedIndex, 0);
  assert.equal(state.composerScopedCount, 0);
  assert.equal(state.accept, '.txt');
  assert.equal(state.multiple, false);
});

test('attachment upload state reads bounded basenames from the active FileList', async () => {
  const fileNames = ['a.txt', 'b.txt', 'c.txt', 'd.txt'];
  const page = fakeAttachmentUploadStatePage({
    fileNames,
    tileBasenames: fileNames.slice(0, 2),
  });
  const state = await readAttachmentUploadState(page, { expectedBasenames: fileNames });
  assert.equal(state.fileInputOwner, 'unified-composer');
  assert.equal(state.inputFileCount, fileNames.length);
  assert.deepEqual(state.inputFileBasenames, fileNames);
  assert.equal(state.pendingCount, 0);
  assert.equal(state.errorCount, 0);
  assert.equal(state.sendAvailable, true);
  assert.equal(state.readReliable, true);
  assert.equal(isAttachmentUploadReady(state, fileNames.length, fileNames), true);
});

test('MCP attachment schema is optional, bounded, and strict', () => {
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok' }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', attachments: [] }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', attachments: [FIXTURE] }).success, true);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', attachments: [''] }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', attachments: Array(MAX_ATTACHMENTS + 1).fill(FIXTURE) }).success, false);
  assert.equal(consultGptInputSchema.safeParse({ prompt: 'ok', allowed_attachment_roots: [BRIDGE_ROOT] }).success, false);
});

test('MCP handler forwards only attachment paths and keeps empty-list compatibility', async () => {
  const calls = [];
  const handler = createConsultGptHandler({
    consult: async (prompt, options) => {
      calls.push({ prompt, options });
      return {
        consultationId: 'CONSULT-attachment-test',
        responseText: 'OK',
        receiptPath: 'D:/bridge/receipt.json',
        requestCount: 1,
      };
    },
  });
  await handler({ prompt: 'no files', attachments: [] });
  await handler({ prompt: 'one file', attachments: [FIXTURE] });
  assert.deepEqual(calls[0].options.attachments, []);
  assert.deepEqual(calls[1].options.attachments, [FIXTURE]);
});

test('allowed roots are canonicalized from startup configuration', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-roots-'));
  try {
    const link = path.join(rootDir, 'root-link');
    await fs.symlink(rootDir, link, 'junction');
    const roots = await resolveAllowedAttachmentRoots({ allowedAttachmentRoots: [link] });
    assert.deepEqual(roots, [await fs.realpath(rootDir)]);
    const envRoots = await resolveAllowedAttachmentRoots({ env: rootDir });
    assert.deepEqual(envRoots, [await fs.realpath(rootDir)]);
    assert.equal(ATTACHMENT_ROOTS_ENV, 'CHATGPT_ALLOWED_ATTACHMENT_ROOTS');
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('attachment path preparation enforces file, root, symlink, sensitive, readable, and size boundaries', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-security-'));
  const allowedDir = path.join(rootDir, 'allowed');
  const outsideDir = path.join(rootDir, 'outside');
  await fs.mkdir(allowedDir, { recursive: true });
  await fs.mkdir(outsideDir, { recursive: true });
  const validPath = path.join(allowedDir, 'valid.txt');
  const outsidePath = path.join(outsideDir, 'outside.txt');
  const envPath = path.join(allowedDir, '.env');
  const profilePath = path.join(allowedDir, '.auth', 'private.txt');
  const arbitraryProfileDir = path.join(allowedDir, 'custom-browser-profile-7f3');
  const loginDataPath = path.join(arbitraryProfileDir, 'Login Data');
  const preferencesPath = path.join(arbitraryProfileDir, 'Preferences');
  const profileCookiesPath = path.join(arbitraryProfileDir, 'Cookies');
  const directoryPath = path.join(allowedDir, 'directory');
  const symlinkPath = path.join(allowedDir, 'escape.txt');
  const largePath = path.join(allowedDir, 'large.txt');
  await fs.writeFile(validPath, 'safe attachment\n', 'utf8');
  await fs.writeFile(outsidePath, 'outside\n', 'utf8');
  await fs.writeFile(envPath, 'PRIVATE=not-for-gpt\n', 'utf8');
  await fs.mkdir(path.dirname(profilePath), { recursive: true });
  await fs.writeFile(profilePath, 'profile data\n', 'utf8');
  await fs.mkdir(arbitraryProfileDir, { recursive: true });
  // These are synthetic filenames/content only; no real browser profile is read.
  await fs.writeFile(loginDataPath, 'synthetic login database\n', 'utf8');
  await fs.writeFile(preferencesPath, '{"synthetic":true}\n', 'utf8');
  await fs.writeFile(profileCookiesPath, 'synthetic cookies\n', 'utf8');
  await fs.mkdir(directoryPath);
  await fs.writeFile(largePath, '0123456789', 'utf8');
  try {
    await fs.symlink(outsidePath, symlinkPath, 'file');
  } catch (error) {
    await removeTestRoot(rootDir);
    if (error?.code === 'EPERM' || error?.code === 'EACCES') return;
    throw error;
  }
  try {
    const roots = await resolveAllowedAttachmentRoots({ allowedAttachmentRoots: [allowedDir] });
    const prepared = await validateAttachmentPath(validPath, { allowedAttachmentRoots: roots });
    assert.equal(prepared.basename, 'valid.txt');
    assert.equal(prepared.relativePath, 'valid.txt');
    assert.equal(prepared.byteSize, Buffer.byteLength('safe attachment\n'));
    assert.match(prepared.sha256, /^[0-9a-f]{64}$/);
    assert.equal(prepared.mediaType, 'text/plain');

    await assert.rejects(
      validateAttachmentPath(path.join(allowedDir, 'missing.txt'), { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_NOT_FOUND,
    );
    await assert.rejects(
      validateAttachmentPath(directoryPath, { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_NOT_REGULAR_FILE,
    );
    await assert.rejects(
      validateAttachmentPath(outsidePath, { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
    );
    await assert.rejects(
      validateAttachmentPath(symlinkPath, { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
    );
    await assert.rejects(
      validateAttachmentPath(envPath, { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
    );
    await assert.rejects(
      validateAttachmentPath(profilePath, { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
    );
    for (const profileFile of [loginDataPath, preferencesPath, profileCookiesPath]) {
      await assert.rejects(
        validateAttachmentPath(profileFile, {
          allowedAttachmentRoots: roots,
          profileDir: arbitraryProfileDir,
        }),
        (error) => error.code === FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      );
    }
    await assert.rejects(
      validateAttachmentPath(largePath, { allowedAttachmentRoots: roots, maxBytes: 4 }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_SIZE_EXCEEDED,
    );
    await assert.rejects(
      prepareAttachments(Array(MAX_ATTACHMENTS + 1).fill(validPath), { allowedAttachmentRoots: roots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_COUNT_EXCEEDED,
    );
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('upload-ready helper requires UI markers, no pending/error state, and a usable composer', () => {
  const ready = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 1,
    inputFileBasenames: ['attachment_nonce.txt'],
    chipCount: 1,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    readReliable: true,
    tileBasenames: ['attachment_nonce.txt'],
    matchedBasenames: ['attachment_nonce.txt'],
    attachmentTileRecords: [{
      visible: true,
      basenameCandidates: ['attachment_nonce.txt'],
      attachmentContainer: true,
      hasAttachmentOperation: true,
    }],
  };
  assert.equal(isAttachmentUploadReady(ready, 1, ['attachment_nonce.txt']), true);
  assert.equal(isAttachmentUploadReady({ ...ready, pendingCount: 1 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, errorCount: 1 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, composerReady: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, sendControlPresent: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, sendControlPresent: undefined }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, sendAvailable: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, pendingCount: 1 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, fileInputPresent: false, inputFileCount: 0 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, inputFileCount: 0, inputFileBasenames: [] }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, inputFileBasenames: ['wrong.txt'] }, 1, ['attachment_nonce.txt']), false);
  const wrongAttachmentRecord = (basename) => [{
    visible: true,
    basenameCandidates: [basename],
    attachmentContainer: true,
    hasAttachmentOperation: true,
  }];
  assert.equal(isAttachmentUploadReady({ ...ready, tileBasenames: ['old.txt'], attachmentTileRecords: wrongAttachmentRecord('old.txt') }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, tileBasenames: ['old-attachment_nonce.txt'], attachmentTileRecords: wrongAttachmentRecord('old-attachment_nonce.txt') }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, tileBasenames: ['attachment_nonce.txt', 'Remove file', 'Download'] }, 1, ['attachment_nonce.txt']), true);
  assert.equal(isAttachmentUploadReady({ ...ready, tileBasenames: ['attachment_nonce.txt', 'Remove file'], matchedBasenames: ['old.txt'] }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, progressPresent: true, progressCompleted: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...ready, progressPresent: true, progressCompleted: true }, 1, ['attachment_nonce.txt']), true);
  assert.equal(isComposerSendable({ sendControlPresent: true, sendAvailable: false }), false);
  assert.equal(isComposerSendable({ sendControlPresent: true, sendAvailable: true }), true);
  assert.equal(isComposerSendable({ sendControlPresent: undefined, sendAvailable: true }), false);
});

test('upload readiness fails closed when reliable tile/basename state is missing or malformed', () => {
  const state = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 1,
    inputFileBasenames: ['attachment_nonce.txt'],
    readReliable: true,
    chipCount: 1,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    tileBasenames: ['attachment_nonce.txt'],
    matchedBasenames: ['attachment_nonce.txt'],
  };
  assert.equal(isAttachmentUploadReady({ ...state, readReliable: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, fileInputPresent: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, fileInputOwner: 'global-fallback' }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, inputFileCount: 0 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, inputFileBasenames: undefined }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, inputFileBasenames: ['other.txt'] }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, tileBasenames: undefined }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, tileBasenames: 'attachment_nonce.txt' }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, matchedBasenames: undefined }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, matchedBasenames: 'attachment_nonce.txt' }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, pendingCount: 1 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, errorCount: 1 }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, sendAvailable: false }, 1, ['attachment_nonce.txt']), false);
  assert.equal(isAttachmentUploadReady({ ...state, progressPresent: true, progressCompleted: false }, 1, ['attachment_nonce.txt']), false);
});

test('upload readiness normalizes ChatGPT timestamped duplicate basenames', () => {
  const state = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 2,
    inputFileBasenames: [
      'LATEST_RESULT(20260904-051748).md',
      'STAGE_CONTEXT(20260904-051748).md',
    ],
    readReliable: true,
    chipCount: 2,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    tileBasenames: [
      'LATEST_RESULT(20260904-051748).md',
      'STAGE_CONTEXT(20260904-051748).md',
    ],
    matchedBasenames: [
      'LATEST_RESULT(20260904-051748).md',
      'STAGE_CONTEXT(20260904-051748).md',
    ],
  };
  assert.equal(
    isAttachmentUploadReady(state, 2, ['LATEST_RESULT.md', 'STAGE_CONTEXT.md']),
    true,
  );
});

test('upload readiness accepts virtualized chips when the active FileList is complete', () => {
  const expected = ['a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt', 'f.txt'];
  const state = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: expected.length,
    inputFileBasenames: expected,
    readReliable: true,
    chipCount: 3,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    // Only the currently mounted/visible summary chips are exposed.
    tileBasenames: expected.slice(0, 3),
    matchedBasenames: expected.slice(0, 3),
  };
  assert.equal(isAttachmentUploadReady(state, expected.length, expected), true);
  const diagnostics = attachmentReadinessDiagnostics(state, expected.length, expected, { finalPredicate: true });
  assert.equal(diagnostics.file_input_basenames_matched, true);
  assert.equal(diagnostics.expected_basenames_matched, false);
  assert.equal(diagnostics.visible_chip_count, 3);
});

test('upload readiness ignores file-like page text without attachment semantics', async () => {
  const expected = ['expected-a.txt', 'expected-b.txt', 'expected-c.txt', 'expected-d.txt', 'expected-e.txt', 'expected-f.txt'];
  const extraneous = ['page-a.txt', 'page-b.txt', 'page-c.txt', 'page-d.txt', 'page-e.txt', 'page-f.txt'];
  const tileRecords = [
    ...expected.map((basename) => ({
      visible: true,
      candidates: [basename],
      basenameCandidates: [basename],
      attachmentContainer: true,
      hasAttachmentOperation: true,
    })),
    ...extraneous.map((basename) => ({
      visible: true,
      candidates: [basename],
      basenameCandidates: [basename],
      // These are ordinary visible page text candidates.  They deliberately
      // have no clear attachment container or operation/status signal.
      attachmentContainer: false,
      hasAttachmentOperation: false,
      hasUploadStatus: false,
    })),
  ];
  const state = await readAttachmentUploadState(
    fakeAttachmentUploadStatePage({ fileNames: expected, tileRecords }),
    { expectedBasenames: expected },
  );
  assert.equal(state.chipCount, expected.length + extraneous.length);
  assert.equal(state.attachmentCandidateCount, expected.length + extraneous.length);
  assert.equal(state.attachmentSemanticCount, expected.length);
  assert.equal(state.attachmentContradictionCount, 0);
  assert.equal(state.attachmentContradictionReason, 'none');
  assert.equal(isAttachmentUploadReady(state, expected.length, expected), true);
  const diagnostics = attachmentReadinessDiagnostics(state, expected.length, expected, { finalPredicate: true });
  assert.equal(diagnostics.visible_attachment_candidate_count, 12);
  assert.equal(diagnostics.visible_attachment_semantic_count, 6);
  assert.equal(diagnostics.visible_attachment_contradiction_count, 0);
  assert.equal(diagnostics.attachment_contradiction_reason, 'none');
});

test('only a visible wrong attachment container with an operation or upload status contradicts', () => {
  const expected = ['expected.txt'];
  const ready = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 1,
    inputFileBasenames: expected,
    readReliable: true,
    chipCount: 1,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    tileBasenames: ['wrong.txt'],
    matchedBasenames: expected,
  };
  const explicitWrong = (extra = {}) => ({
    ...ready,
    attachmentTileRecords: [{
      visible: true,
      basenameCandidates: ['wrong.txt'],
      attachmentContainer: true,
      ...extra,
    }],
  });
  assert.equal(
    isAttachmentUploadReady(explicitWrong({ hasAttachmentOperation: true }), 1, expected),
    false,
  );
  const contradictionDiagnostics = attachmentReadinessDiagnostics(
    explicitWrong({ hasAttachmentOperation: true }),
    1,
    expected,
  );
  assert.equal(contradictionDiagnostics.visible_attachment_candidate_count, 1);
  assert.equal(contradictionDiagnostics.visible_attachment_semantic_count, 1);
  assert.equal(contradictionDiagnostics.visible_attachment_contradiction_count, 1);
  assert.equal(contradictionDiagnostics.attachment_contradiction_reason, 'visible_wrong_attachment_basename');
  assert.doesNotMatch(JSON.stringify(contradictionDiagnostics), /innerText|outerHTML|DOM|cookie/i);
  assert.equal(
    isAttachmentUploadReady(explicitWrong({ hasUploadStatus: true }), 1, expected),
    false,
  );
  // The core status checks remain fail-closed independently of contradiction
  // evidence, including a remove/progress/error transition.
  assert.equal(
    isAttachmentUploadReady({ ...explicitWrong(), errorCount: 1 }, 1, expected),
    false,
  );
  assert.equal(
    isAttachmentUploadReady({ ...explicitWrong(), progressPresent: true, progressCompleted: false }, 1, expected),
    false,
  );
  assert.equal(
    isAttachmentUploadReady({ ...explicitWrong({ hasAttachmentOperation: true }), pendingCount: 1 }, 1, expected),
    false,
  );
  assert.equal(
    isAttachmentUploadReady({ ...ready, attachmentTileRecords: [{
      visible: true,
      basenameCandidates: ['wrong.txt'],
      attachmentContainer: true,
    }] }, 1, expected),
    true,
    'without an operation/status signal the wrong basename remains supplementary',
  );
  assert.equal(
    isAttachmentUploadReady({ ...explicitWrong({ hasAttachmentOperation: true }), attachmentTileRecords: [{
      visible: true,
      basenameCandidates: ['wrong.txt'],
      attachmentContainer: true,
      hasAttachmentOperation: true,
      supplementary: true,
    }] }, 1, expected),
    true,
    'summary/virtualized/history candidates remain supplementary even with file-like text',
  );
  assert.equal(
    isAttachmentUploadReady({ ...explicitWrong({ hasAttachmentOperation: true }), attachmentTileRecords: [{
      visible: false,
      basenameCandidates: ['wrong.txt'],
      attachmentContainer: true,
      hasAttachmentOperation: true,
    }] }, 1, expected),
    true,
    'hidden candidates cannot contradict the active FileList',
  );
});

test('attachment readiness core FileList and controls fail closed', () => {
  const expected = ['expected.txt'];
  const ready = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 1,
    inputFileBasenames: expected,
    readReliable: true,
    chipCount: 1,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
    tileBasenames: expected,
    matchedBasenames: expected,
  };
  for (const variant of [
    { fileInputPresent: false },
    { fileInputOwner: 'none' },
    { inputFileCount: 0, inputFileBasenames: [] },
    { inputFileCount: 2 },
    { pendingCount: 1 },
    { errorCount: 1 },
    { sendControlPresent: false },
    { sendAvailable: false },
  ]) {
    assert.equal(isAttachmentUploadReady({ ...ready, ...variant }, 1, expected), false);
  }
  assert.equal(isAttachmentUploadReady({ ...ready, inputFileBasenames: undefined }, 1, expected), false);
});

test('attachment readiness diagnostics are bounded and omit DOM/file contents', async () => {
  const state = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: 1,
    inputFileBasenames: ['attachment_nonce.txt'],
    chipCount: 1,
    pendingCount: 1,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: false,
    progressPresent: true,
    progressCompleted: false,
    readReliable: true,
    tileBasenames: ['attachment_nonce.txt'],
    matchedBasenames: ['attachment_nonce.txt'],
  };
  const diagnostics = attachmentReadinessDiagnostics(state, 2, ['attachment_nonce.txt', 'missing.txt'], {
    elapsedMs: 17,
    finalPredicate: false,
    mode: 'fresh',
    requestCount: 0,
  });
  assert.deepEqual(diagnostics, {
    requested_count: 2,
    file_input_present: true,
    file_input_count: 1,
    file_input_accepted: false,
    file_input_total_count: null,
    file_input_selected_index: null,
    file_input_owner: 'unified-composer',
    file_input_accept: null,
    file_input_multiple: null,
    composer_scoped_input_count: null,
    file_input_basename_count: 1,
    file_input_basenames_matched: false,
    visible_chip_count: 1,
    expected_basename_count: 2,
    matched_basename_count: 1,
    expected_basenames_matched: false,
    visible_attachment_candidate_count: 1,
    visible_attachment_semantic_count: 0,
    visible_attachment_contradiction_count: 0,
    attachment_contradiction_reason: 'none',
    pending_count: 1,
    progress_present: true,
    progress_completed: false,
    error_ui: false,
    error_count: 0,
    composer_ready: true,
    send_control_present: true,
    send_enabled: false,
    read_reliable: true,
    final_predicate: false,
    elapsed_wait_ms: 17,
    expected_basenames: ['attachment_nonce.txt', 'missing.txt'],
    file_input_basenames: ['attachment_nonce.txt'],
    tile_basenames: ['attachment_nonce.txt'],
    matched_basenames: ['attachment_nonce.txt'],
    mode: 'fresh',
    request_count: 0,
  });
  assert.doesNotMatch(JSON.stringify(diagnostics), /cookie|token|DOM|innerText/i);

  let failure;
  await assert.rejects(
    waitForAttachmentsReady({}, 2, {
      expectedBasenames: ['attachment_nonce.txt', 'missing.txt'],
      timeoutMs: 10,
      pollMs: 1,
      readState: async () => state,
      sleep: async () => {},
      diagnosticContext: { mode: 'fresh', requestCount: 0 },
    }),
    (error) => {
      failure = error;
      return /Timed out waiting for ChatGPT attachments/.test(error.message);
    },
  );
  assert.equal(failure.diagnostics.requested_count, 2);
  assert.equal(failure.diagnostics.final_predicate, false);
  assert.doesNotMatch(failure.message, /tileBasenames|innerText/i);
});

test('attachment DOM read exceptions return an explicit unreliable state', async () => {
  const page = {
    locator() { throw new Error('detached DOM'); },
    getByPlaceholder() { throw new Error('detached DOM'); },
    getByRole() { throw new Error('detached DOM'); },
  };
  const state = await readAttachmentUploadState(page, { expectedBasenames: ['attachment_nonce.txt'] });
  assert.equal(state.readReliable, false);
  assert.equal(state.pendingCount, 0);
  assert.equal(state.errorCount, 0);
  assert.equal(isAttachmentUploadReady(state, 1, ['attachment_nonce.txt']), false);
});

test('upload-ready polling is DOM-state driven and fails on an upload error', async () => {
  const states = [
    { fileInputPresent: true, fileInputOwner: 'unified-composer', inputFileCount: 1, inputFileBasenames: ['x.txt'], chipCount: 1, pendingCount: 1, errorCount: 0, composerReady: true, sendControlPresent: true, sendAvailable: true, progressPresent: false, progressCompleted: false, readReliable: true, tileBasenames: ['x.txt'], matchedBasenames: ['x.txt'] },
    { fileInputPresent: true, fileInputOwner: 'unified-composer', inputFileCount: 1, inputFileBasenames: ['x.txt'], chipCount: 1, pendingCount: 0, errorCount: 0, composerReady: true, sendControlPresent: true, sendAvailable: true, progressPresent: false, progressCompleted: false, readReliable: true, tileBasenames: ['x.txt'], matchedBasenames: ['x.txt'] },
  ];
  let index = 0;
  const result = await waitForAttachmentsReady({}, 1, {
    expectedBasenames: ['x.txt'],
    timeoutMs: 100,
    pollMs: 1,
    readState: async () => states[Math.min(index++, states.length - 1)],
    sleep: async () => {},
  });
  assert.equal(result.pendingCount, 0);
  await assert.rejects(
    waitForAttachmentsReady({}, 1, {
      timeoutMs: 20,
      pollMs: 1,
      readState: async () => ({ ...states[0], pendingCount: 0, errorCount: 1 }),
      sleep: async () => {},
    }),
    /upload error/i,
  );
  await assert.rejects(
    waitForAttachmentsReady({}, 1, {
      timeoutMs: 20,
      pollMs: 1,
      readState: async () => ({ ...states[1], readReliable: false }),
      sleep: async () => {},
    }),
    /unreliable/i,
  );
});

test('attachment readiness timeout performs one bounded active-input reattach before prompt use', async () => {
  const expected = ['first.txt', 'second.txt'];
  const page = fakeReattachPage();
  const readyState = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: expected.length,
    inputFileBasenames: expected,
    readReliable: true,
    tileBasenames: expected,
    matchedBasenames: expected,
    chipCount: expected.length,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
  };
  let stateReads = 0;
  const result = await uploadAttachments(page, ['/tmp/first.txt', '/tmp/second.txt'], {
    expectedBasenames: expected,
    timeoutMs: 100,
    pollMs: 1,
    reattachAfterMs: 5,
    reattachSettleMs: 0,
    readState: async () => {
      stateReads += 1;
      return page.selectionCount < 2
        ? { ...readyState, sendControlPresent: false, sendAvailable: false, tileBasenames: [], matchedBasenames: [], chipCount: 0 }
        : readyState;
    },
    sleep: async () => {},
  });
  assert.equal(result, readyState);
  assert.ok(stateReads > 1);
  assert.equal(page.selectionCount, 2);
  assert.deepEqual(page.events.map((event) => event.files), [[], ['/tmp/first.txt', '/tmp/second.txt'], [], ['/tmp/first.txt', '/tmp/second.txt']]);
  assert.equal(result.attachmentDiagnostics.reattach_attempted, true);
  assert.equal(result.attachmentDiagnostics.reattach_succeeded, true);
  assert.equal(result.attachmentDiagnostics.reattach_attempt_count, 1);
  assert.ok(result.attachmentDiagnostics.reattach_elapsed_ms >= 0);
});

test('failed reattach cleanup waits for the existing attachment set before failing', async () => {
  const expected = ['first.txt', 'second.txt'];
  const page = fakeRetryCleanupSettlingPage();
  const readyState = {
    fileInputPresent: true,
    fileInputOwner: 'unified-composer',
    inputFileCount: expected.length,
    inputFileBasenames: expected,
    readReliable: true,
    tileBasenames: expected,
    matchedBasenames: expected,
    chipCount: expected.length,
    pendingCount: 0,
    errorCount: 0,
    composerReady: true,
    sendControlPresent: true,
    sendAvailable: true,
    progressPresent: false,
    progressCompleted: false,
  };
  const pendingState = {
    ...readyState,
    pendingCount: 1,
    sendAvailable: false,
  };

  const result = await uploadAttachments(page, ['/tmp/first.txt', '/tmp/second.txt'], {
    expectedBasenames: expected,
    timeoutMs: 80,
    pollMs: 1,
    reattachAfterMs: 5,
    reattachSettleMs: 0,
    readState: async () => page.phase === 'settled' ? readyState : pendingState,
    sleep: async (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  });

  assert.equal(result, readyState);
  assert.equal(page.uploadCount, 1);
  assert.equal(result.attachmentDiagnostics.reattach_attempted, true);
  assert.equal(result.attachmentDiagnostics.reattach_succeeded, false);
  assert.equal(result.attachmentDiagnostics.existing_settle_attempted, true);
  assert.equal(result.attachmentDiagnostics.existing_settle_succeeded, true);
});

test('initial attachment cleanup failure is fail-closed before selecting new files', async () => {
  const page = fakeAttachmentPage({ clearError: new Error('input cleanup failed') });
  await assert.rejects(
    uploadAttachments(page, ['new.txt'], {
      expectedBasenames: ['new.txt'],
      timeoutMs: 10,
      sleep: async () => {},
    }),
    /input cleanup failed|cleanup left files/i,
  );
  assert.deepEqual(page.calls.map((call) => call.type), ['setInputFiles']);
  assert.deepEqual(page.calls[0].files, []);
});

test('stale composer tile or failed remove control aborts before new upload', async () => {
  for (const page of [
    fakeAttachmentPage({ visibleTiles: 1 }),
    fakeAttachmentPage({ visibleTiles: 1, removeCount: 1, removeError: new Error('remove failed') }),
  ]) {
    await assert.rejects(
      uploadAttachments(page, ['new.txt'], { timeoutMs: 10, sleep: async () => {} }),
      /cleanup|remove failed/i,
    );
    assert.equal(page.calls.some((call) => Array.isArray(call.files) && call.files.includes('new.txt')), false);
  }
});

test('attachment baseline comparison rejects deletion of an existing logical slot', () => {
  const before = [
    { id: 'old-1', slotId: 'slot-1', text: 'old one' },
    { id: 'old-2', slotId: 'slot-2', text: 'old two' },
  ];
  assert.throws(
    () => assertAssistantBaselineUnchanged(before, [before[0]]),
    (error) => error.code === FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
  );
});

test('receipt attachment metadata is safe and complete', () => {
  const receipt = buildReceipt({
    consultationId: 'CONSULT-20260903-000000-aabbccdd',
    createdAt: '2026-09-03T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    chatUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    mode: 'fresh',
    conversationId: CONVERSATION_ID,
    conversationRootConsultationId: 'CONSULT-20260903-000000-aabbccdd',
    conversationValidated: true,
    status: 'complete',
    requestCount: 1,
    attachments: [{
      basename: 'attachment_nonce.txt',
      relative_path: 'test/fixtures/attachment_nonce.txt',
      byte_size: 42,
      sha256: 'a'.repeat(64),
      media_type: 'text/plain',
      upload_status: 'ready',
    }],
  });
  assert.equal(validateReceiptAttachments(receipt.attachments, { complete: true }), true);
  assert.equal(receipt.attachments[0].relative_path.includes('D:'), false);
  assert.equal(JSON.stringify(receipt).includes('realPath'), false);
});

test('failure receipt diagnostics keep only bounded readiness fields', () => {
  const receipt = buildReceipt({
    consultationId: 'CONSULT-20260904-000000-aabbccdd',
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: 'fresh',
    status: 'failed_before_prompt',
    requestCount: 0,
    diagnostics: {
      requested_count: 3,
      file_input_present: true,
      file_input_count: 1,
      file_input_accepted: false,
      visible_chip_count: 1,
      visible_attachment_candidate_count: 13,
      visible_attachment_semantic_count: 6,
      visible_attachment_contradiction_count: 0,
      attachment_contradiction_reason: 'none',
      expected_basename_count: 3,
      matched_basename_count: 1,
      expected_basenames_matched: false,
      pending_count: 1,
      progress_present: true,
      progress_completed: false,
      error_ui: false,
      error_count: 0,
      composer_ready: true,
      send_control_present: true,
      send_enabled: false,
      read_reliable: true,
      final_predicate: false,
      elapsed_wait_ms: 30000,
      mode: 'fresh',
      request_count: 0,
      tileBasenames: ['secret-cookie.txt'],
      cookie: 'must-not-appear',
      dom_dump: '<html>must-not-appear</html>',
    },
  });
  assert.equal(receipt.attachment_diagnostics.requested_count, 3);
  assert.equal(receipt.attachment_diagnostics.file_input_accepted, false);
  assert.equal(receipt.attachment_diagnostics.visible_attachment_candidate_count, 13);
  assert.equal(receipt.attachment_diagnostics.visible_attachment_semantic_count, 6);
  assert.equal(receipt.attachment_diagnostics.visible_attachment_contradiction_count, 0);
  assert.equal(receipt.attachment_diagnostics.attachment_contradiction_reason, 'none');
  assert.doesNotMatch(JSON.stringify(receipt), /secret-cookie|must-not-appear|dom_dump|cookie/i);
});

test('receipt attachment paths reject Windows drive and UNC absolute forms', () => {
  const base = {
    basename: 'attachment_nonce.txt',
    relative_path: 'test/fixtures/attachment_nonce.txt',
    byte_size: 42,
    sha256: 'a'.repeat(64),
    media_type: 'text/plain',
    upload_status: 'ready',
  };
  for (const relativePath of [
    'C:\\secret\\attachment_nonce.txt',
    'C:/secret/attachment_nonce.txt',
    '\\\\server\\share\\attachment_nonce.txt',
    '//server/share/attachment_nonce.txt',
    '\\rooted\\attachment_nonce.txt',
  ]) {
    assert.equal(
      validateReceiptAttachments([{ ...base, relative_path: relativePath }], { complete: true }),
      false,
      relativePath,
    );
  }
});

test('invalid attachment fails before browser creation and records request_count=0', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-presend-'));
  const outside = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-outside-'));
  const outsidePath = path.join(outside, 'outside.txt');
  await fs.writeFile(outsidePath, 'outside', 'utf8');
  let factoryCalls = 0;
  try {
    await assert.rejects(
      consultOnce('must not send', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        allowedAttachmentRoots: [rootDir],
        attachments: [outsidePath],
        bridgeFactory: () => {
          factoryCalls += 1;
          throw new Error('browser must not open');
        },
      }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
    );
    assert.equal(factoryCalls, 0);
    const consultationDirs = await fs.readdir(path.join(rootDir, '.consultations'));
    assert.equal(consultationDirs.length, 1);
    const receiptPath = path.join(rootDir, '.consultations', consultationDirs[0], 'receipt.json');
    const receipt = JSON.parse(await fs.readFile(receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.status, 'failed_before_prompt');
    assert.equal(receipt.attachments[0].upload_status, 'rejected');
    assert.equal(JSON.stringify(receipt).includes(outsidePath), false);
  } finally {
    await removeTestRoot(rootDir);
    await removeTestRoot(outside);
  }
});

test('upload failure is fail-closed, leaves request_count=0, and never sends partial files', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-upload-failure-'));
  const fake = fakeBridge({
    uploadError: new BridgeError(FAILURE_CODES.ATTACHMENT_UPLOAD_FAILED, 'simulated partial upload'),
  });
  try {
    await assert.rejects(
      consultOnce('must not send after upload failure', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        allowedAttachmentRoots: [BRIDGE_ROOT],
        attachments: [FIXTURE, FIXTURE],
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_UPLOAD_FAILED,
    );
    assert.equal(fake.bridge.requestCount, 0);
    assert.equal(fake.events.some((event) => event.startsWith('send:')), false);
    const consultationDirs = await fs.readdir(path.join(rootDir, '.consultations'));
    const receipt = JSON.parse(await fs.readFile(path.join(rootDir, '.consultations', consultationDirs[0], 'receipt.json'), 'utf8'));
    assert.equal(receipt.status, 'failed_before_prompt');
    assert.equal(receipt.request_count, 0);
    assert.deepEqual(receipt.attachments.map((item) => item.upload_status), ['failed', 'failed']);
  } finally {
    await removeTestRoot(rootDir);
  }
});

test('successful text attachment records hash/type/ready metadata and sends once', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-attachment-success-'));
  const fake = fakeBridge();
  try {
    const result = await consultOnce('read the attachment', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      allowedAttachmentRoots: [BRIDGE_ROOT],
      attachments: [FIXTURE],
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(fake.events.filter((event) => event.startsWith('send:')).length, 1);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.attachments.length, 1);
    assert.equal(receipt.attachments[0].basename, 'attachment_nonce.txt');
    assert.equal(receipt.attachments[0].relative_path, 'test/fixtures/attachment_nonce.txt');
    assert.equal(receipt.attachments[0].media_type, 'text/plain');
    assert.equal(receipt.attachments[0].upload_status, 'ready');
    assert.equal(receipt.attachments[0].sha256, crypto.createHash('sha256').update(await fs.readFile(FIXTURE)).digest('hex'));
    assert.equal(validateReceiptAttachments(receipt.attachments, { complete: true }), true);
  } finally {
    await removeTestRoot(rootDir);
  }
});
