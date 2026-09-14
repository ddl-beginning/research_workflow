import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  createFreshConversation,
  findComposer,
  hasLoginIndicator,
  waitForComposerOrLogin,
} from '../src/chatgpt-ui.mjs';

const PROJECT_URL = 'https://chatgpt.com/g/g-p-test-project/project';
const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';

function fakeLocator(visible) {
  const locator = {
    async count() {
      return visible ? 1 : 0;
    },
    first() {
      return locator;
    },
    nth() {
      return locator;
    },
    async isVisible() {
      return visible;
    },
  };
  return locator;
}

function fakePage({
  url = PROJECT_URL,
  loginButtonVisible = false,
  loginLinkVisible = false,
  broadLoginTextVisible = false,
  composerVisible = false,
} = {}) {
  const calls = [];
  const locators = {
    button: fakeLocator(loginButtonVisible),
    link: fakeLocator(loginLinkVisible),
    textbox: fakeLocator(composerVisible),
    placeholder: fakeLocator(composerVisible),
    broadText: fakeLocator(broadLoginTextVisible),
    fallback: fakeLocator(composerVisible),
  };
  return {
    calls,
    composer: locators.placeholder,
    url() {
      calls.push('url');
      return url;
    },
    getByRole(role) {
      calls.push(`role:${role}`);
      return locators[role] || fakeLocator(false);
    },
    getByPlaceholder() {
      calls.push('placeholder');
      return locators.placeholder;
    },
    getByText() {
      calls.push('broad-text');
      return locators.broadText;
    },
    locator() {
      calls.push('locator');
      return locators.fallback;
    },
    async waitForTimeout() {},
  };
}

function fakeFreshConversationPage({
  beforeUrl = PROJECT_URL,
  routeSequence = [],
} = {}) {
  let currentUrl = beforeUrl;
  let clickCount = 0;
  const waitCalls = [];
  const routes = [...routeSequence];
  const control = {
    async count() {
      return 1;
    },
    first() {
      return control;
    },
    nth() {
      return control;
    },
    async isVisible() {
      return true;
    },
    async evaluate() {
      return 'auto';
    },
    async click() {
      clickCount += 1;
    },
  };
  return {
    page: {
      url() {
        return currentUrl;
      },
      getByRole(role) {
        return role === 'button' ? control : fakeLocator(false);
      },
      locator() {
        return fakeLocator(false);
      },
      async waitForTimeout(ms) {
        waitCalls.push(ms);
        if (routes.length > 0) currentUrl = routes.shift();
      },
    },
    get clickCount() {
      return clickCount;
    },
    waitCalls,
  };
}

test('fresh conversation waits for a changed route with a verifiable conversation id', async () => {
  const afterUrl = `https://chatgpt.com/c/${CONVERSATION_ID}`;
  const fake = fakeFreshConversationPage({
    routeSequence: [PROJECT_URL, afterUrl],
  });

  const result = await createFreshConversation(fake.page, { settleMs: 100, pollMs: 0 });

  assert.equal(result.clicked, true);
  assert.equal(result.beforeUrl, PROJECT_URL);
  assert.equal(result.afterUrl, afterUrl);
  assert.equal(result.conversationId, CONVERSATION_ID);
  assert.equal(fake.clickCount, 1);
  assert.equal(fake.waitCalls.length, 2);
});

test('fresh conversation reports an unverified route after the bounded wait', async () => {
  const fake = fakeFreshConversationPage();

  const result = await createFreshConversation(fake.page, { settleMs: 0, pollMs: 0 });

  assert.equal(result.clicked, true);
  assert.equal(result.afterUrl, PROJECT_URL);
  assert.equal(result.conversationId, null);
  assert.equal(fake.clickCount, 1);
  assert.deepEqual(fake.waitCalls, []);
});

test('homepage fresh composer remains deferred until the first prompt creates its route', async () => {
  const fake = fakeFreshConversationPage({
    beforeUrl: 'https://chatgpt.com/',
  });

  const result = await createFreshConversation(fake.page, { settleMs: 0, pollMs: 0 });

  // The UI helper itself still reports an unverified route; the bridge layer
  // performs the stronger homepage-empty-state verification before deferring.
  assert.equal(result.clicked, true);
  assert.equal(result.deferred, undefined);
  assert.equal(result.afterUrl, 'https://chatgpt.com/');
  assert.equal(result.conversationId, null);
  assert.equal(fake.clickCount, 1);
});

test('Project landing preserves scope and defers fresh route creation to the first prompt', async () => {
  const fake = fakeFreshConversationPage();

  const result = await createFreshConversation(fake.page, {
    preserveProjectScope: true,
    settleMs: 100,
    pollMs: 0,
  });

  assert.equal(result.clicked, false);
  assert.equal(result.deferred, true);
  assert.equal(result.beforeUrl, PROJECT_URL);
  assert.equal(result.afterUrl, PROJECT_URL);
  assert.equal(result.conversationId, null);
  assert.equal(fake.clickCount, 0);
  assert.deepEqual(fake.waitCalls, []);
});

test('authenticated Project page ignores broad login text and keeps its composer usable', async () => {
  const page = fakePage({
    broadLoginTextVisible: true,
    composerVisible: true,
  });

  assert.equal(await hasLoginIndicator(page), false);
  const result = await waitForComposerOrLogin(page, { timeoutMs: 10, pollMs: 0 });
  assert.equal(result.loginRequired, false);
  assert.equal(result.composer, page.composer);
  assert.equal(page.calls.includes('broad-text'), false);
});

test('composer discovery accepts the localized Project textbox label', async () => {
  const composer = fakeLocator(true);
  const page = {
    getByPlaceholder() {
      return fakeLocator(false);
    },
    getByRole(role, options) {
      if (role === 'textbox') {
        assert.ok(options?.name instanceof RegExp);
        assert.match(options.name.source, /聊天/);
        return composer;
      }
      return fakeLocator(false);
    },
    locator() {
      return fakeLocator(false);
    },
  };

  assert.equal(await findComposer(page), composer);
});

test('normal ChatGPT login URL remains a login-required fail-closed state', async () => {
  const page = fakePage({
    url: 'https://chatgpt.com/auth/login?next=%2F',
    broadLoginTextVisible: true,
  });

  assert.equal(await hasLoginIndicator(page), true);
  const result = await waitForComposerOrLogin(page, { timeoutMs: 10, pollMs: 0 });
  assert.equal(result.composer, null);
  assert.equal(result.loginRequired, true);
});

test('explicit login controls remain indicators even outside the login route', async () => {
  const page = fakePage({
    loginButtonVisible: true,
  });

  assert.equal(await hasLoginIndicator(page), true);
});

test('a missing composer on a non-login page does not become a false login signal', async () => {
  const page = fakePage();

  const result = await waitForComposerOrLogin(page, { timeoutMs: 0, pollMs: 0 });
  assert.equal(result.composer, null);
  assert.equal(result.loginRequired, false);
});
