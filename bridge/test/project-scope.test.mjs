import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  buildReceipt,
  BROWSER_PROCESS_EXIT_TIMEOUT_MS,
  BROWSER_PROCESS_KILL_WAIT_MS,
  ChatGPTBridge,
  BridgeError,
  CONVERSATION_MODES,
  consultOnce,
  extractConversationIdFromUrl,
  FAILURE_CODES,
  isValidProjectConversationUrl,
  isValidProjectUrl,
  MAX_PROJECT_NAVIGATION_ELAPSED_MS,
  PROJECT_NAVIGATION_FAILURE_CLASSES,
  PROJECT_NAVIGATION_RETRY_SETTLE_MS,
  normalizeProjectUrl,
  safeProjectNavigationDiagnostics,
  sanitizeProjectNavigationUrl,
  validateContinuationReceipt,
} from '../src/bridge.mjs';

const PROJECT_URL = 'https://chatgpt.com/g/g-p-example-project/project';
const OTHER_PROJECT_URL = 'https://chatgpt.com/g/g-p-other-project/project';
const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';
const PROJECT_CONVERSATION_URL = `https://chatgpt.com/g/g-p-example-project/c/${CONVERSATION_ID}`;
const ROOT_ID = 'CONSULT-20260904-000000-aabbccdd';
const BRIDGE_ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));

function fakeBridge({
  initialUrl = 'https://chatgpt.com/',
  afterFreshUrl = `https://chatgpt.com/c/${CONVERSATION_ID}`,
  finalUrl = afterFreshUrl,
  navigateToError,
  navigateToLandingUrl,
} = {}) {
  const events = [];
  let activeProjectUrl = null;
  let promptSent = false;
  const projectLandingFromRoute = (value) => {
    if (typeof value !== 'string') return null;
    const match = value.match(/^(https:\/\/chatgpt\.com\/g\/g-p-[A-Za-z0-9][A-Za-z0-9._~-]*)\/(?:project|c\/[^/?#]+)(?:[/?#].*)?$/);
    return match ? `${match[1]}/project` : null;
  };
  const bridge = {
    requestCount: 0,
    async open() { events.push('open'); },
    async navigate() { events.push('navigate'); return initialUrl; },
    async navigateTo(url) {
      events.push(`navigateTo:${url}`);
      activeProjectUrl = projectLandingFromRoute(url);
      if (navigateToError) throw navigateToError;
      return navigateToLandingUrl === undefined ? url : navigateToLandingUrl;
    },
    async ensureLoggedIn() { events.push('ensureLoggedIn'); },
    async waitForAssistantBaseline(options = {}) {
      events.push(`baseline:${options.requireNonEmpty ? 'non-empty' : 'empty-ok'}`);
      return options.requireNonEmpty ? [{ id: 'existing', text: 'existing' }] : [];
    },
    async createFreshConversation() {
      events.push('createFreshConversation');
      if (activeProjectUrl) return { clicked: false, deferred: true, beforeUrl: initialUrl, afterUrl: activeProjectUrl };
      return { clicked: true, beforeUrl: initialUrl, afterUrl: afterFreshUrl };
    },
    async sendOnePrompt() { events.push('send'); promptSent = true; this.requestCount += 1; return 'PROJECT_SCOPE_OK'; },
    currentUrl() {
      if (activeProjectUrl && !promptSent && finalUrl === afterFreshUrl && !finalUrl.includes('/g/g-p-')) {
        return activeProjectUrl;
      }
      if (activeProjectUrl && promptSent && finalUrl === afterFreshUrl && !finalUrl.includes('/g/g-p-')) {
        return `${activeProjectUrl.replace(/\/project$/, '')}/c/${CONVERSATION_ID}`;
      }
      return finalUrl;
    },
    async close() { events.push('close'); },
    releaseForManualLogin() { events.push('releaseForManualLogin'); },
  };
  return { bridge, events };
}

function fakeProjectPage(outcomes) {
  let currentUrl = 'about:blank';
  let currentTitle = '';
  const gotoCalls = [];
  const settleCalls = [];
  const page = {
    async goto(url, options) {
      gotoCalls.push({ url, options });
      const outcome = outcomes.shift();
      if (!outcome) throw new Error('unexpected extra navigation');
      currentUrl = outcome.landingUrl || currentUrl;
      currentTitle = outcome.title || '';
      if (outcome.error) throw outcome.error;
      return outcome.response === undefined ? null : outcome.response;
    },
    url() { return currentUrl; },
    async title() { return currentTitle; },
    async waitForTimeout(ms) { settleCalls.push(ms); },
  };
  return { page, gotoCalls, settleCalls };
}

function bridgeWithPage(page) {
  const bridge = new ChatGPTBridge({ navigationTimeoutMs: 60_000 });
  bridge.page = page;
  bridge.context = {};
  return bridge;
}

function bootstrappedProjectPage() {
  let currentUrl = 'about:blank';
  const gotoCalls = [];
  const locator = (kind, visible = true) => ({
    async count() { return kind === 'project-link' ? 0 : 1; },
    first() { return this; },
    nth() { return this; },
    async isVisible() { return visible; },
    async click() {},
  });
  const page = {
    async goto(url, options) {
      gotoCalls.push({ url, options });
      currentUrl = url;
      return { status: () => 200 };
    },
    url() { return currentUrl; },
    async title() { return currentUrl === 'https://chatgpt.com/' ? 'ChatGPT' : 'Project'; },
    async waitForTimeout() {},
    async waitForURL(predicate) {
      assert.equal(predicate(new URL(currentUrl)), true);
    },
    getByPlaceholder() { return locator('composer'); },
    getByRole(_role, options = {}) {
      const name = String(options.name || '');
      return locator(name.match(/message|prompt|chat|问问|聊天|消息/i) ? 'composer' : 'login', false);
    },
    locator(selector) { return locator(selector.startsWith('a[href=') ? 'project-link' : 'composer'); },
  };
  return { page, gotoCalls };
}

function responseWithStatus(status) {
  return { status: () => status };
}

function projectReceipt({ projectUrl = PROJECT_URL, chatUrl = PROJECT_CONVERSATION_URL, consultationId = ROOT_ID } = {}) {
  return buildReceipt({
    consultationId,
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    conversationId: CONVERSATION_ID,
    chatUrl,
    conversationRootConsultationId: consultationId,
    conversationValidated: true,
    status: 'complete',
    responseCharCount: 16,
    projectUrl,
    projectScopeRequested: true,
    projectScopeVerified: true,
    projectScopeEvidence: {
      initial_navigation: {
        requested_url: projectUrl,
        landed_url: projectUrl,
        matched: true,
        verified: true,
      },
    },
  });
}

async function createReceiptRoot(receipt) {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-scope-'));
  const receiptDir = path.join(rootDir, '.consultations', receipt.consultation_id);
  await fs.mkdir(receiptDir, { recursive: true });
  const receiptPath = path.join(receiptDir, 'receipt.json');
  await fs.writeFile(receiptPath, `${JSON.stringify(receipt)}\n`, 'utf8');
  return { rootDir, receiptPath };
}

test('project URL validation accepts only safe trusted-origin targets', () => {
  assert.equal(normalizeProjectUrl(`${PROJECT_URL}/`), PROJECT_URL);
  assert.equal(isValidProjectUrl(PROJECT_URL), true);
  for (const value of [
    'https://evil.example/g/g-p-project/project',
    'http://chatgpt.com/g/g-p-project/project',
    '/g/g-p-project/project',
    `${PROJECT_URL}?token=must-not-be-stored`,
    `${PROJECT_URL}#fragment`,
    'https://chatgpt.com/',
    'https://chatgpt.com/projects/research-tools',
    `https://chatgpt.com/c/${CONVERSATION_ID}`,
    'https://chatgpt.com/g/g-p-project/../project',
  ]) {
    assert.equal(isValidProjectUrl(value), false, value);
  }
});

test('new receipts bind the target mode and fresh Project chat marker', () => {
  const scoped = projectReceipt({
    projectUrl: PROJECT_URL,
    chatUrl: PROJECT_CONVERSATION_URL,
  });
  assert.equal(scoped.chatgpt_target_mode, 'PROJECT');
  assert.equal(scoped.chatgpt_target_origin, 'https://chatgpt.com');
  assert.match(scoped.chatgpt_target_url_digest, /^[0-9a-f]{64}$/);
  assert.equal(scoped.chatgpt_project_target_verified, 'YES');
  assert.equal(scoped.fresh_project_chat_created, 'YES');
  assert.equal(validateContinuationReceipt(scoped, ROOT_ID), true);

  const defaultReceipt = buildReceipt({
    consultationId: ROOT_ID,
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    conversationId: CONVERSATION_ID,
    chatUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    conversationRootConsultationId: ROOT_ID,
    conversationValidated: true,
    status: 'complete',
    responseCharCount: 16,
  });
  assert.equal(defaultReceipt.chatgpt_target_mode, 'DEFAULT');
  assert.equal(defaultReceipt.chatgpt_target_url_digest, null);
  assert.equal(defaultReceipt.chatgpt_project_target_verified, 'NO');
  assert.equal(defaultReceipt.fresh_project_chat_created, 'NO');
  assert.equal(validateContinuationReceipt(defaultReceipt, ROOT_ID), true);
});

test('conversation identity accepts the nested route emitted by a Project composer', () => {
  const nestedUrl = `https://chatgpt.com/g/g-p-example-project/c/${CONVERSATION_ID}`;
  assert.equal(extractConversationIdFromUrl(nestedUrl), CONVERSATION_ID);
  assert.equal(extractConversationIdFromUrl(`${nestedUrl}?view=latest#reply`), CONVERSATION_ID);
  assert.equal(extractConversationIdFromUrl('https://chatgpt.com/g/other-project/c/not-a-uuid'), null);
  assert.equal(extractConversationIdFromUrl('https://evil.example/g/g-p-project/c/' + CONVERSATION_ID), null);
});

test('Project continuation route validation requires the exact bound slug and conversation route', () => {
  assert.equal(isValidProjectConversationUrl(PROJECT_CONVERSATION_URL, PROJECT_URL, CONVERSATION_ID), true);
  assert.equal(isValidProjectConversationUrl(`https://chatgpt.com/c/${CONVERSATION_ID}`, PROJECT_URL, CONVERSATION_ID), false);
  assert.equal(isValidProjectConversationUrl(
    `https://chatgpt.com/g/g-p-other-project/c/${CONVERSATION_ID}`,
    PROJECT_URL,
    CONVERSATION_ID,
  ), false);
  assert.equal(isValidProjectConversationUrl(
    `${PROJECT_CONVERSATION_URL}?view=latest`,
    PROJECT_URL,
    CONVERSATION_ID,
  ), false);
  assert.equal(isValidProjectConversationUrl(
    PROJECT_CONVERSATION_URL,
    PROJECT_URL,
    '12345678-1234-4234-8234-123456789abd',
  ), false);
});

test('project navigation retries one timeout or network failure, then succeeds without changing request_count', async () => {
  const transientFailures = [
    Object.assign(new Error('page.goto: Timeout 60000ms exceeded.'), { name: 'TimeoutError' }),
    new Error('page.goto: net::ERR_CONNECTION_RESET at https://chatgpt.com/'),
  ];
  for (const error of transientFailures) {
    const fixture = fakeProjectPage([
      { error, landingUrl: 'https://chatgpt.com/' },
      {
        response: responseWithStatus(200),
        landingUrl: PROJECT_URL,
        title: 'Project landing',
      },
    ]);
    const bridge = bridgeWithPage(fixture.page);
    const landedUrl = await bridge.navigateToProject(PROJECT_URL);
    assert.equal(landedUrl, PROJECT_URL);
    assert.equal(fixture.gotoCalls.length, 2);
    assert.deepEqual(fixture.gotoCalls[0].options, fixture.gotoCalls[1].options);
    assert.deepEqual(fixture.settleCalls, [
      PROJECT_NAVIGATION_RETRY_SETTLE_MS,
      PROJECT_NAVIGATION_RETRY_SETTLE_MS,
    ]);
    assert.equal(bridge.requestCount, 0);
    assert.deepEqual(bridge.projectNavigationDiagnostics, {
      requested_url: PROJECT_URL,
      attempt_count: 2,
      retry_count: 1,
      elapsed_ms: bridge.projectNavigationDiagnostics.elapsed_ms,
      http_status: 200,
      title: 'Project landing',
      landing_url: PROJECT_URL,
      failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.NONE,
      error_hash: null,
    });
    assert.ok(bridge.projectNavigationDiagnostics.elapsed_ms >= 0);
    assert.ok(bridge.projectNavigationDiagnostics.elapsed_ms <= MAX_PROJECT_NAVIGATION_ELAPSED_MS);
  }
});

test('project navigation fails closed after one transient retry and exposes bounded diagnostic hash', async () => {
  const first = new Error('page.goto: Timeout 60000ms exceeded.');
  first.name = 'TimeoutError';
  const second = new Error('page.goto: net::ERR_CONNECTION_RESET with raw-token=must-not-appear');
  const fixture = fakeProjectPage([
    { error: first, landingUrl: 'https://chatgpt.com/' },
    { error: second, landingUrl: 'https://chatgpt.com/' },
  ]);
  const bridge = bridgeWithPage(fixture.page);
  await assert.rejects(
    bridge.navigateToProject(PROJECT_URL),
    (error) => {
      assert.equal(error.code, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
      assert.deepEqual(error.diagnostics, bridge.projectNavigationDiagnostics);
      assert.equal(error.diagnostics.attempt_count, 2);
      assert.equal(error.diagnostics.retry_count, 1);
      assert.equal(error.diagnostics.failure_class, PROJECT_NAVIGATION_FAILURE_CLASSES.NETWORK);
      assert.match(error.diagnostics.error_hash, /^[0-9a-f]{64}$/);
      assert.doesNotMatch(JSON.stringify(error.diagnostics), /raw-token|must-not-appear/);
      return true;
    },
  );
  assert.equal(fixture.gotoCalls.length, 2);
  assert.equal(bridge.requestCount, 0);
});

test('target-closed project navigation recycles the page/context before its one retry', async () => {
  const targetClosed = Object.assign(
    new Error('Target page, context or browser has been closed'),
    { name: 'TargetClosedError' },
  );
  const first = fakeProjectPage([{ error: targetClosed }]);
  const second = fakeProjectPage([{
    response: responseWithStatus(200),
    landingUrl: PROJECT_URL,
    title: 'Project landing',
  }]);
  const bridge = bridgeWithPage(first.page);
  const lifecycle = [];
  bridge.close = async () => {
    lifecycle.push('close');
    bridge.page = null;
    bridge.context = null;
    return true;
  };
  bridge.open = async () => {
    lifecycle.push('open');
    bridge.page = second.page;
    bridge.context = { recovered: true };
  };

  const landedUrl = await bridge.navigateToProject(PROJECT_URL);
  assert.equal(landedUrl, PROJECT_URL);
  assert.equal(first.gotoCalls.length, 1);
  assert.equal(second.gotoCalls.length, 1);
  assert.notEqual(first.page, second.page);
  assert.deepEqual(lifecycle, ['close', 'open']);
  assert.equal(bridge.requestCount, 0);
  assert.equal(bridge.projectNavigationDiagnostics.attempt_count, 2);
  assert.equal(bridge.projectNavigationDiagnostics.retry_count, 1);
  assert.equal(bridge.projectNavigationDiagnostics.failure_class, PROJECT_NAVIGATION_FAILURE_CLASSES.NONE);
});

test('target-closed recovery failure is terminal before any prompt or second navigation', async () => {
  const targetClosed = Object.assign(
    new Error('Target page, context or browser has been closed'),
    { name: 'TargetClosedError' },
  );
  const first = fakeProjectPage([{ error: targetClosed }]);
  const bridge = bridgeWithPage(first.page);
  bridge.close = async () => {
    bridge.page = null;
    bridge.context = null;
    return true;
  };
  bridge.open = async () => {
    throw new Error('Target page has been closed during recovery');
  };

  await assert.rejects(
    bridge.navigateToProject(PROJECT_URL),
    (error) => {
      assert.equal(error.code, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
      assert.equal(error.requestCount, undefined);
      assert.equal(error.diagnostics.attempt_count, 1);
      assert.equal(error.diagnostics.retry_count, 1);
      assert.equal(error.diagnostics.failure_class, PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED);
      return true;
    },
  );
  assert.equal(first.gotoCalls.length, 1);
  assert.equal(bridge.requestCount, 0);
});

test('a second target-closed page remains terminal after the one recovery attempt', async () => {
  const targetClosed = () => Object.assign(
    new Error('Target page, context or browser has been closed'),
    { name: 'TargetClosedError' },
  );
  const first = fakeProjectPage([{ error: targetClosed() }]);
  const second = fakeProjectPage([{ error: targetClosed() }]);
  const bridge = bridgeWithPage(first.page);
  bridge.close = async () => {
    bridge.page = null;
    bridge.context = null;
    return true;
  };
  bridge.open = async () => {
    bridge.page = second.page;
    bridge.context = { recovered: true };
  };

  await assert.rejects(
    bridge.navigateToProject(PROJECT_URL),
    (error) => {
      assert.equal(error.code, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
      assert.equal(error.diagnostics.attempt_count, 2);
      assert.equal(error.diagnostics.retry_count, 1);
      assert.equal(error.diagnostics.failure_class, PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED);
      return true;
    },
  );
  assert.equal(first.gotoCalls.length, 1);
  assert.equal(second.gotoCalls.length, 1);
  assert.notEqual(first.page, second.page);
  assert.equal(bridge.requestCount, 0);
});

test('close waits for an owned browser process before returning and never kills a clean exit', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.killed = false;
  child.kill = () => {
    child.killed = true;
    return true;
  };
  const browser = {
    async close() {
      queueMicrotask(() => {
        child.exitCode = 0;
        child.signalCode = null;
        child.emit('exit', 0, null);
        child.emit('close', 0, null);
      });
    },
  };
  const bridge = new ChatGPTBridge();
  bridge.browser = browser;
  bridge.browserProcess = child;
  bridge.context = {};
  bridge.page = {};
  const startedAt = Date.now();
  const result = await bridge.close();
  assert.equal(result, true);
  assert.equal(child.killed, false);
  assert.equal(bridge.browser, null);
  assert.equal(bridge.browserProcess, null);
  assert.ok(Date.now() - startedAt < BROWSER_PROCESS_EXIT_TIMEOUT_MS + BROWSER_PROCESS_KILL_WAIT_MS);
});

test('project navigation treats HTTP 403 and other 4xx responses as terminal without retry', async () => {
  for (const status of [403, 404]) {
    const fixture = fakeProjectPage([{
      response: responseWithStatus(status),
      landingUrl: PROJECT_URL,
      title: status === 403 ? 'Challenge token=must-not-appear' : 'Not found',
    }]);
    const bridge = bridgeWithPage(fixture.page);
    await assert.rejects(
      bridge.navigateTo(PROJECT_URL, FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
      (error) => {
        assert.equal(error.code, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
        assert.equal(error.diagnostics.http_status, status);
        assert.equal(
          error.diagnostics.failure_class,
          status === 403
            ? PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE
            : PROJECT_NAVIGATION_FAILURE_CLASSES.HTTP_ERROR,
        );
        assert.equal(error.diagnostics.attempt_count, 1);
        assert.equal(error.diagnostics.retry_count, 0);
        assert.doesNotMatch(JSON.stringify(error.diagnostics), /must-not-appear/);
        return true;
      },
    );
    assert.equal(fixture.gotoCalls.length, 1);
    assert.equal(bridge.requestCount, 0);
  }
});

test('project navigation diagnostics reach the failure receipt without sending a prompt', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-navigation-receipt-'));
  const fixture = fakeProjectPage([{
    response: responseWithStatus(403),
    landingUrl: PROJECT_URL,
    title: 'Challenge token=must-not-appear',
  }]);
  const bridge = bridgeWithPage(fixture.page);
  let sendCount = 0;
  bridge.open = async () => {};
  bridge.close = async () => {};
  bridge.ensureLoggedIn = async () => {};
  bridge.createFreshConversation = async () => ({ clicked: true, afterUrl: `https://chatgpt.com/c/${CONVERSATION_ID}` });
  bridge.waitForAssistantBaseline = async () => [];
  bridge.sendOnePrompt = async () => {
    sendCount += 1;
    bridge.requestCount += 1;
    return 'not expected';
  };
  bridge.currentUrl = () => PROJECT_CONVERSATION_URL;
  try {
    await assert.rejects(
      consultOnce('must not send after project challenge', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: PROJECT_URL,
        bridgeFactory: () => bridge,
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_NAVIGATION_FAILED && error.requestCount === 0,
    );
    assert.equal(sendCount, 0);
    assert.equal(fixture.gotoCalls.length, 1);
    const consultationDirs = await fs.readdir(path.join(rootDir, '.consultations'), { withFileTypes: true });
    assert.equal(consultationDirs.length, 1);
    const receipt = JSON.parse(await fs.readFile(
      path.join(rootDir, '.consultations', consultationDirs[0].name, 'receipt.json'),
      'utf8',
    ));
    assert.equal(receipt.failure_code, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.project_navigation_diagnostics.http_status, 403);
    assert.equal(receipt.project_navigation_diagnostics.failure_class, PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE);
    assert.doesNotMatch(JSON.stringify(receipt), /must-not-appear|cookie|storage/i);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('real Playwright-shaped project navigation bootstraps home before the bound Project route', async () => {
  const fixture = bootstrappedProjectPage();
  const bridge = bridgeWithPage(fixture.page);
  const landed = await bridge.navigateToProject(PROJECT_URL);
  assert.equal(landed, PROJECT_URL);
  assert.deepEqual(fixture.gotoCalls.map((item) => item.url), [
    'https://chatgpt.com/',
    PROJECT_URL,
  ]);
  assert.equal(bridge.projectNavigationDiagnostics.bootstrap_used, true);
  assert.equal(bridge.projectNavigationDiagnostics.bootstrap_url, 'https://chatgpt.com/');
  assert.equal(bridge.projectNavigationDiagnostics.landing_url, PROJECT_URL);
});

test('project navigation retry does not consume the one prompt budget in a successful consultation', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-navigation-success-'));
  const timeout = Object.assign(new Error('page.goto: Timeout 60000ms exceeded.'), { name: 'TimeoutError' });
  const fixture = fakeProjectPage([
    { error: timeout, landingUrl: 'https://chatgpt.com/' },
    { response: responseWithStatus(200), landingUrl: PROJECT_URL, title: 'Project landing' },
  ]);
  const bridge = bridgeWithPage(fixture.page);
  let sendCount = 0;
  bridge.open = async () => {};
  bridge.close = async () => {};
  bridge.ensureLoggedIn = async () => {};
  bridge.createFreshConversation = async () => ({ clicked: true, afterUrl: `https://chatgpt.com/c/${CONVERSATION_ID}` });
  bridge.waitForAssistantBaseline = async () => [];
  bridge.sendOnePrompt = async () => {
    sendCount += 1;
    bridge.requestCount += 1;
    return 'PROJECT_RETRY_OK';
  };
  bridge.currentUrl = () => PROJECT_CONVERSATION_URL;
  try {
    const result = await consultOnce('send once after navigation retry', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      projectUrl: PROJECT_URL,
      bridgeFactory: () => bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(sendCount, 1);
    assert.equal(fixture.gotoCalls.length, 2);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 1);
    assert.equal(receipt.project_navigation_diagnostics.attempt_count, 2);
    assert.equal(receipt.project_navigation_diagnostics.retry_count, 1);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('project navigation invalid URL fails before goto and mismatch is rejected without retry', async () => {
  const invalidFixture = fakeProjectPage([]);
  const invalidBridge = bridgeWithPage(invalidFixture.page);
  await assert.rejects(
    invalidBridge.navigateToProject(`${PROJECT_URL}?token=must-not-appear`),
    (error) => error.code === FAILURE_CODES.PROJECT_URL_INVALID,
  );
  assert.equal(invalidFixture.gotoCalls.length, 0);
  assert.equal(invalidBridge.requestCount, 0);

  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-mismatch-'));
  const fake = fakeBridge({ navigateToLandingUrl: `${PROJECT_URL}/?redirect=must-not-appear` });
  try {
    await assert.rejects(
      consultOnce('project mismatch', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: PROJECT_URL,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_SCOPE_MISMATCH && error.requestCount === 0,
    );
    assert.equal(fake.events.filter((event) => event === `navigateTo:${PROJECT_URL}`).length, 1);
    assert.equal(fake.events.includes('send'), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('project navigation diagnostics sanitizer keeps only bounded fields and strips credential material', () => {
  const diagnostics = safeProjectNavigationDiagnostics({
    requested_url: `${PROJECT_URL}?token=must-not-appear`,
    landing_url: `${PROJECT_URL}?cookie=must-not-appear#fragment`,
    attempt_count: 99,
    retry_count: 99,
    elapsed_ms: Number.MAX_SAFE_INTEGER,
    http_status: 403,
    title: `Challenge token=must-not-appear ${'x'.repeat(500)}`,
    failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE,
    error_hash: 'A'.repeat(64),
    dom_dump: 'must-not-appear',
    cookie: 'must-not-appear',
  });
  assert.deepEqual(diagnostics, {
    requested_url: PROJECT_URL,
    landing_url: PROJECT_URL,
    attempt_count: 2,
    retry_count: 1,
    elapsed_ms: MAX_PROJECT_NAVIGATION_ELAPSED_MS,
    http_status: 403,
    title: `Challenge token=<redacted> ${'x'.repeat(500)}`.slice(0, 160),
    failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE,
    error_hash: 'a'.repeat(64),
  });
  assert.equal(sanitizeProjectNavigationUrl(`${PROJECT_URL}?token=must-not-appear#fragment`), PROJECT_URL);
  assert.doesNotMatch(JSON.stringify(diagnostics), /must-not-appear|dom_dump|cookie/);
});

test('fresh project consultation lands in project before login and fresh creation', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-fresh-'));
  const fake = fakeBridge();
  try {
    const result = await consultOnce('project prompt', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.FRESH,
      projectUrl: PROJECT_URL,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(result.projectUrl, PROJECT_URL);
    assert.equal(result.projectScopeVerified, true);
    assert.deepEqual(fake.events.slice(0, 4), [
      'open',
      `navigateTo:${PROJECT_URL}`,
      'ensureLoggedIn',
      'createFreshConversation',
    ]);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.project_url, PROJECT_URL);
    assert.equal(receipt.project_scope_requested, true);
    assert.equal(receipt.project_scope_verified, true);
    assert.equal(receipt.project_scope_evidence.initial_navigation.matched, true);
    assert.doesNotMatch(JSON.stringify(receipt), /cookie|token|storage|secret/i);
    assert.equal(validateContinuationReceipt(receipt, result.consultationId), true);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('pre-prompt failure recovers with a fresh bridge cycle without a semantic resend', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-pre-prompt-recovery-'));
  const first = fakeBridge({
    navigateToError: new BridgeError(
      FAILURE_CODES.PROMPT_INPUT_NOT_FOUND,
      'synthetic frontend bootstrap failure',
    ),
  });
  const second = fakeBridge();
  const bridges = [first.bridge, second.bridge];
  let factoryCalls = 0;
  try {
    const result = await consultOnce('recover one intent', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      projectUrl: PROJECT_URL,
      bridgeFactory: () => bridges[factoryCalls++],
    });
    assert.equal(factoryCalls, 2);
    assert.equal(result.requestCount, 1);
    assert.deepEqual(result.pre_prompt_recovery, {
      attempted: true,
      cycles: 1,
      max_cycles: 2,
      failure_codes: [FAILURE_CODES.PROMPT_INPUT_NOT_FOUND],
    });
    assert.equal(first.events.includes('send'), false);
    assert.equal(second.events.filter((event) => event === 'send').length, 1);
    const consultationDirs = await fs.readdir(path.join(rootDir, '.consultations'), { withFileTypes: true });
    assert.equal(consultationDirs.length, 2);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('bound Project consultations reject homepage fallback before opening the bridge', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-homepage-fallback-'));
  let factoryCalls = 0;
  try {
    await assert.rejects(
      consultOnce('must stay in project', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: PROJECT_URL,
        transport: 'homepage_fallback',
        bridgeFactory: () => {
          factoryCalls += 1;
          throw new Error('bridge must not open');
        },
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_SCOPE_REQUIRED && error.requestCount === 0,
    );
    assert.equal(factoryCalls, 0);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('a global conversation route after send is rejected without a second prompt', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-post-send-scope-'));
  const fake = fakeBridge();
  const originalSend = fake.bridge.sendOnePrompt.bind(fake.bridge);
  fake.bridge.sendOnePrompt = async (...args) => {
    const response = await originalSend(...args);
    fake.bridge.currentUrl = () => `https://chatgpt.com/c/${CONVERSATION_ID}`;
    return response;
  };
  try {
    await assert.rejects(
      consultOnce('reject escaped route', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: PROJECT_URL,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_SCOPE_MISMATCH && error.requestCount === 1,
    );
    assert.equal(fake.events.filter((event) => event === 'send').length, 1);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('continue in the same project verifies scope then opens the receipt chat', async () => {
  const parent = projectReceipt();
  const { rootDir } = await createReceiptRoot(parent);
  const fake = fakeBridge({
    initialUrl: parent.chat_url,
    finalUrl: parent.chat_url,
  });
  try {
    const result = await consultOnce('continue project prompt', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.CONTINUE,
      continueFrom: parent.consultation_id,
      project_url: PROJECT_URL,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(result.projectUrl, PROJECT_URL);
    assert.equal(result.projectScopeVerified, true);
    assert.deepEqual(fake.events.slice(0, 5), [
      'open',
      `navigateTo:${parent.chat_url}`,
      'ensureLoggedIn',
      'baseline:non-empty',
      'send',
    ]);
    assert.equal(fake.events.includes(`navigateTo:${PROJECT_URL}`), false);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.project_url, PROJECT_URL);
    assert.equal(receipt.parent_consultation_id, parent.consultation_id);
    assert.equal(receipt.project_scope_requested, true);
    assert.equal(receipt.project_scope_verified, true);
    assert.deepEqual(receipt.project_scope_evidence.parent_binding, { matched: true, verified: true });
    assert.equal(validateContinuationReceipt(receipt, result.consultationId), true);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('continue without project_url inherits the parent project binding', async () => {
  const parent = projectReceipt();
  const { rootDir } = await createReceiptRoot(parent);
  const fake = fakeBridge({ finalUrl: parent.chat_url });
  try {
    const result = await consultOnce('inherit project prompt', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.CONTINUE,
      continueFrom: parent.consultation_id,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    assert.equal(result.projectUrl, PROJECT_URL);
    assert.equal(result.projectScopeVerified, true);
    assert.deepEqual(fake.events.slice(0, 5), [
      'open',
      `navigateTo:${parent.chat_url}`,
      'ensureLoggedIn',
      'baseline:non-empty',
      'send',
    ]);
    assert.equal(fake.events.includes(`navigateTo:${PROJECT_URL}`), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('Project continuation rejects global or wrong-slug parent chat routes before opening a browser', async () => {
  const cases = [
    {
      label: 'global route',
      chatUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    },
    {
      label: 'wrong project slug',
      chatUrl: `https://chatgpt.com/g/g-p-other-project/c/${CONVERSATION_ID}`,
    },
  ];
  for (const item of cases) {
    const parent = projectReceipt({ chatUrl: item.chatUrl });
    const { rootDir } = await createReceiptRoot(parent);
    const fake = fakeBridge();
    try {
      await assert.rejects(
        consultOnce(`reject ${item.label}`, {
          rootDir,
          profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
          mode: CONVERSATION_MODES.CONTINUE,
          continueFrom: parent.consultation_id,
          projectUrl: PROJECT_URL,
          bridgeFactory: () => fake.bridge,
        }),
        (error) => error.code === FAILURE_CODES.PROJECT_SCOPE_MISMATCH && error.requestCount === 0,
      );
      assert.equal(fake.events.includes('open'), false, item.label);
      assert.equal(fake.events.some((event) => event.startsWith('navigateTo:')), false, item.label);
    } finally {
      await fs.rm(rootDir, { recursive: true, force: true });
    }
  }
});

test('Project continuation rejects a landed conversation identity mismatch without sending', async () => {
  const parent = projectReceipt();
  const wrongConversationId = '12345678-1234-4234-8234-123456789abd';
  const wrongLandingUrl = `https://chatgpt.com/g/g-p-example-project/c/${wrongConversationId}`;
  const { rootDir } = await createReceiptRoot(parent);
  const fake = fakeBridge({
    finalUrl: wrongLandingUrl,
    navigateToLandingUrl: wrongLandingUrl,
  });
  try {
    await assert.rejects(
      consultOnce('reject identity mismatch', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        mode: CONVERSATION_MODES.CONTINUE,
        continueFrom: parent.consultation_id,
        projectUrl: PROJECT_URL,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH && error.requestCount === 0,
    );
    assert.deepEqual(fake.events.slice(0, 2), ['open', `navigateTo:${parent.chat_url}`]);
    assert.equal(fake.events.includes('send'), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('continue rejects a cross-project request before opening a browser', async () => {
  const parent = projectReceipt();
  const { rootDir } = await createReceiptRoot(parent);
  const fake = fakeBridge();
  try {
    await assert.rejects(
      consultOnce('cross-project prompt', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        mode: CONVERSATION_MODES.CONTINUE,
        continueFrom: parent.consultation_id,
        projectUrl: OTHER_PROJECT_URL,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_SCOPE_MISMATCH && error.requestCount === 0,
    );
    assert.equal(fake.events.includes('open'), false);
    assert.equal(fake.events.some((event) => event.startsWith('navigateTo:')), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('invalid project URL fails closed before browser creation', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-invalid-'));
  let factoryCalls = 0;
  try {
    await assert.rejects(
      consultOnce('invalid project prompt', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: 'https://evil.example/projects/not-a-project',
        bridgeFactory: () => {
          factoryCalls += 1;
          throw new Error('bridge must not be created');
        },
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_URL_INVALID && error.requestCount === 0,
    );
    assert.equal(factoryCalls, 0);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('project navigation errors are classified and never send a prompt', async () => {
  const rootDir = await fs.mkdtemp(path.join(BRIDGE_ROOT, '.test-project-navigation-'));
  const fake = fakeBridge({ navigateToError: new Error('navigation unavailable') });
  try {
    await assert.rejects(
      consultOnce('navigation failure', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
        projectUrl: PROJECT_URL,
        bridgeFactory: () => fake.bridge,
      }),
      (error) => error.code === FAILURE_CODES.PROJECT_NAVIGATION_FAILED && error.requestCount === 0,
    );
    assert.equal(fake.events.includes('send'), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});

test('legacy receipts without project scope remain valid and continue-compatible', async () => {
  const legacy = buildReceipt({
    consultationId: ROOT_ID,
    createdAt: '2026-09-04T00:00:00.000Z',
    profile: '.auth/chatgpt-profile',
    mode: CONVERSATION_MODES.FRESH,
    conversationId: CONVERSATION_ID,
    chatUrl: `https://chatgpt.com/c/${CONVERSATION_ID}`,
    conversationRootConsultationId: ROOT_ID,
    conversationValidated: true,
    status: 'complete',
    responseCharCount: 8,
  });
  assert.equal(validateContinuationReceipt(legacy, ROOT_ID), true);
  const { rootDir } = await createReceiptRoot(legacy);
  const fake = fakeBridge({ finalUrl: legacy.chat_url });
  try {
    const result = await consultOnce('legacy continue', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'chatgpt-profile'),
      mode: CONVERSATION_MODES.CONTINUE,
      continueFrom: ROOT_ID,
      bridgeFactory: () => fake.bridge,
    });
    assert.equal(result.requestCount, 1);
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(Object.hasOwn(receipt, 'project_url'), false);
  } finally {
    await fs.rm(rootDir, { recursive: true, force: true });
  }
});
