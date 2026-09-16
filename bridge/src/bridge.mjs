import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import fs from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import {
  contextPackReceiptMetadata,
  validateContextPackForConsult,
} from './context-pack.mjs';
import {
  clearAttachmentFiles,
  createFreshConversation,
  findComposer,
  findSendButton,
  readAssistantSnapshot,
  uploadAttachments,
  validateStableAssistantSnapshot,
  waitForStableAssistantSnapshot,
  waitForComposerOrLogin,
  waitForNewAssistantResponse,
  RESPONSE_FAILURE_CLASSES,
} from './chatgpt-ui.mjs';

export const CHATGPT_URL = 'https://chatgpt.com/';
export const CHATGPT_ORIGIN = 'https://chatgpt.com';
// A per-project binding is accepted only as ChatGPT's canonical Project
// landing route. Conversation routes are validated separately against the
// bound Project slug.
export const PROJECT_ROUTE_PATTERN = /^\/g\/(g-p-[A-Za-z0-9][A-Za-z0-9._~-]*)\/project\/?$/;
export const PROJECT_URL_MAX_CHARS = 512;
export const BRIDGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULT_PROFILE_DIR = path.resolve(process.cwd(), '.auth', 'chatgpt-profile');
// Keep the normal one-shot wait near the observed three-minute response time,
// with one explicit five-minute upper bound for every bridge entry point.
export const DEFAULT_RESPONSE_TIMEOUT_MS = 180_000;
export const MAX_RESPONSE_TIMEOUT_MS = 300_000;
export const MAX_CHATGPT_REQUESTS_PER_INVOCATION = 1;
export const MAX_ATTACHMENTS = 9;
// Project navigation is allowed one browser-navigation-only retry.  This is
// deliberately separate from the one-prompt budget: retrying page.goto must
// never retry semantic Stage work, upload, or sendOnePrompt().
export const MAX_PROJECT_NAVIGATION_RETRIES = 1;
// A failed navigation or pre-prompt UI preparation may be retried by the
// consultation wrapper, but never by the semantic Stage controller. Two
// fresh browser cycles is the hard infrastructure bound for one intent.
export const MAX_PRE_PROMPT_RECOVERY_CYCLES = 2;
export const PROJECT_NAVIGATION_RETRY_SETTLE_MS = 750;
export const MAX_PROJECT_NAVIGATION_ELAPSED_MS = 120_000;
export const MAX_PROJECT_NAVIGATION_TITLE_CHARS = 160;
// A browser process is owned only when it was spawned by this bridge.  Keep
// shutdown bounded so a failed target cannot make the next consultation wait
// forever on a detached Chromium process or profile lock.
export const BROWSER_PROCESS_EXIT_TIMEOUT_MS = 2_000;
export const BROWSER_PROCESS_KILL_WAIT_MS = 1_000;
// Project pages can finish SPA composer hydration after DOMContentLoaded.
// Keep the wait bounded while allowing the headed profile to settle.
export const COMPOSER_READY_TIMEOUT_MS = 30_000;
// ChatGPT's SPA can finish rendering the assistant turn before it commits the
// conversation URL.  Give that client-side route transition a small bounded
// settling window before the final identity check; this never sends another
// request and never relaxes the expected conversation-id comparison.
export const CONVERSATION_ROUTE_SETTLE_TIMEOUT_MS = 10_000;
export const CONVERSATION_ROUTE_SETTLE_POLL_MS = 50;
// Hydration recovery is navigation-only.  Keep each reload or route restore
// bounded so a transient SPA redirect cannot turn response extraction into an
// unbounded retry loop.
export const CONVERSATION_HYDRATION_NAVIGATION_TIMEOUT_MS = 60_000;
// This is intentionally a local bridge safety cap, not a claim about a
// ChatGPT product limit.  Keep it small enough for a bounded PoC upload.
export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
export const ATTACHMENT_ROOTS_ENV = 'CHATGPT_ALLOWED_ATTACHMENT_ROOTS';
export const DEFAULT_ALLOWED_ATTACHMENT_ROOTS = Object.freeze([BRIDGE_ROOT]);
export const CONVERSATION_MODES = Object.freeze({
  FRESH: 'fresh',
  CONTINUE: 'continue',
});
export const DEFAULT_CONVERSATION_MODE = CONVERSATION_MODES.FRESH;
// `homepage_fallback` is an explicit transport marker for a fresh review
// whose requested Project route could not be treated as a semantic authority.
// Keep the ordinary Project transport available as an explicit value while
// preserving the legacy behaviour when transport is omitted.
export const TRANSPORTS = Object.freeze({
  PROJECT: 'project',
  HOMEPAGE_FALLBACK: 'homepage_fallback',
});
// This alias keeps the naming parallel with CONVERSATION_MODES for callers
// that describe the field as a transport mode.
export const TRANSPORT_MODES = TRANSPORTS;
export const CONSULTATION_ID_PATTERN = /^CONSULT-\d{8}-\d{6}-[0-9a-f]{8}$/i;
export const CONVERSATION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const BROWSER_CHECKPOINT_STATUSES = Object.freeze({
  PASS: 'PASS',
  DEFERRED: 'DEFERRED',
  FAIL: 'FAIL',
  SKIP: 'SKIP',
});

export const FAILURE_CODES = Object.freeze({
  BRIDGE_TIMEOUT: 'BRIDGE_TIMEOUT',
  NETWORK_TRANSIENT: 'NETWORK_TRANSIENT',
  TARGET_CLOSED: 'TARGET_CLOSED',
  LOGIN_REQUIRED: 'LOGIN_REQUIRED',
  CHATGPT_NAVIGATION_FAILED: 'CHATGPT_NAVIGATION_FAILED',
  PROMPT_INPUT_NOT_FOUND: 'PROMPT_INPUT_NOT_FOUND',
  PROMPT_SEND_FAILED: 'PROMPT_SEND_FAILED',
  RESPONSE_TIMEOUT: 'RESPONSE_TIMEOUT',
  RESPONSE_EXTRACTION_FAILED: 'RESPONSE_EXTRACTION_FAILED',
  UNEXPECTED_PAGE_STATE: 'UNEXPECTED_PAGE_STATE',
  CONTINUATION_RECEIPT_NOT_FOUND: 'CONTINUATION_RECEIPT_NOT_FOUND',
  CONTINUATION_RECEIPT_INVALID: 'CONTINUATION_RECEIPT_INVALID',
  CONTINUATION_CHAT_NOT_FOUND: 'CONTINUATION_CHAT_NOT_FOUND',
  FRESH_CHAT_CREATION_FAILED: 'FRESH_CHAT_CREATION_FAILED',
  CONVERSATION_IDENTITY_MISMATCH: 'CONVERSATION_IDENTITY_MISMATCH',
  PROJECT_URL_INVALID: 'PROJECT_URL_INVALID',
  PROJECT_SCOPE_REQUIRED: 'PROJECT_SCOPE_REQUIRED',
  PROJECT_SCOPE_MISMATCH: 'PROJECT_SCOPE_MISMATCH',
  PROJECT_NAVIGATION_FAILED: 'PROJECT_NAVIGATION_FAILED',
  ATTACHMENT_INVALID: 'ATTACHMENT_INVALID',
  ATTACHMENT_NOT_FOUND: 'ATTACHMENT_NOT_FOUND',
  ATTACHMENT_NOT_REGULAR_FILE: 'ATTACHMENT_NOT_REGULAR_FILE',
  ATTACHMENT_NOT_READABLE: 'ATTACHMENT_NOT_READABLE',
  ATTACHMENT_ROOT_VIOLATION: 'ATTACHMENT_ROOT_VIOLATION',
  ATTACHMENT_SENSITIVE_PATH_DENIED: 'ATTACHMENT_SENSITIVE_PATH_DENIED',
  ATTACHMENT_COUNT_EXCEEDED: 'ATTACHMENT_COUNT_EXCEEDED',
  ATTACHMENT_SIZE_EXCEEDED: 'ATTACHMENT_SIZE_EXCEEDED',
  ATTACHMENT_UPLOAD_FAILED: 'ATTACHMENT_UPLOAD_FAILED',
  ATTACHMENT_NOT_READY: 'ATTACHMENT_NOT_READY',
  CONTEXT_PACK_INVALID: 'CONTEXT_PACK_INVALID',
  CONTEXT_PACK_ALREADY_EXISTS: 'CONTEXT_PACK_ALREADY_EXISTS',
  CONTEXT_PACK_SOURCE_NOT_FOUND: 'CONTEXT_PACK_SOURCE_NOT_FOUND',
  CONTEXT_PACK_SOURCE_ROOT_VIOLATION: 'CONTEXT_PACK_SOURCE_ROOT_VIOLATION',
  CONTEXT_PACK_SOURCE_NOT_REGULAR_FILE: 'CONTEXT_PACK_SOURCE_NOT_REGULAR_FILE',
  CONTEXT_PACK_SOURCE_SIZE_EXCEEDED: 'CONTEXT_PACK_SOURCE_SIZE_EXCEEDED',
  CONTEXT_PACK_DUPLICATE_LOGICAL_FILE: 'CONTEXT_PACK_DUPLICATE_LOGICAL_FILE',
  CONTEXT_PACK_ATTACHMENT_COUNT_EXCEEDED: 'CONTEXT_PACK_ATTACHMENT_COUNT_EXCEEDED',
  CONTEXT_PACK_SECRET_REJECTED: 'CONTEXT_PACK_SECRET_REJECTED',
  CONTEXT_PACK_ABSOLUTE_PATH_REJECTED: 'CONTEXT_PACK_ABSOLUTE_PATH_REJECTED',
  CONTEXT_PACK_MANIFEST_INVALID: 'CONTEXT_PACK_MANIFEST_INVALID',
  CONTEXT_PACK_MANIFEST_NOT_FOUND: 'CONTEXT_PACK_MANIFEST_NOT_FOUND',
  CONTEXT_PACK_INTEGRITY_FAILED: 'CONTEXT_PACK_INTEGRITY_FAILED',
});

export const PROJECT_NAVIGATION_FAILURE_CLASSES = Object.freeze({
  NONE: 'none',
  FRONTEND_BOOTSTRAP: 'frontend_bootstrap',
  CHALLENGE: 'challenge',
  HTTP_ERROR: 'http_error',
  TIMEOUT: 'timeout',
  NETWORK: 'network',
  TARGET_CLOSED: 'target_closed',
  INVALID_URL: 'invalid_url',
  URL_MISMATCH: 'url_mismatch',
  UNKNOWN: 'unknown',
});

export class BridgeError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = 'BridgeError';
    this.code = code;
    this.cause = cause;
  }
}

export function isBridgeError(error) {
  return error instanceof BridgeError;
}

export function asBridgeError(error, fallbackCode = FAILURE_CODES.UNEXPECTED_PAGE_STATE) {
  if (isBridgeError(error)) return error;
  return new BridgeError(error?.code || fallbackCode, failureMessage(error?.code || fallbackCode), error);
}

export function normalizeResponseTimeoutMs(value = DEFAULT_RESPONSE_TIMEOUT_MS) {
  const timeoutMs = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_RESPONSE_TIMEOUT_MS) {
    throw new BridgeError(
      FAILURE_CODES.UNEXPECTED_PAGE_STATE,
      `responseTimeoutMs must be a finite positive number no greater than ${MAX_RESPONSE_TIMEOUT_MS}ms.`,
    );
  }
  return timeoutMs;
}

export function resolveResponseTimeoutMs(explicitValue = undefined) {
  const configuredValue = explicitValue === undefined
    ? process.env.CHATGPT_RESPONSE_TIMEOUT_MS
    : explicitValue;
  if (configuredValue === undefined || configuredValue === '') return DEFAULT_RESPONSE_TIMEOUT_MS;
  return normalizeResponseTimeoutMs(configuredValue);
}

function failureMessage(code) {
  return {
    [FAILURE_CODES.BRIDGE_TIMEOUT]: 'The browser bridge timed out before the prompt was sent.',
    [FAILURE_CODES.NETWORK_TRANSIENT]: 'A transient browser network failure occurred before the prompt was sent.',
    [FAILURE_CODES.TARGET_CLOSED]: 'The browser target closed before the prompt was sent.',
    [FAILURE_CODES.LOGIN_REQUIRED]: 'ChatGPT login is required in the headed Chromium profile.',
    [FAILURE_CODES.CHATGPT_NAVIGATION_FAILED]: 'Could not open the ChatGPT page.',
    [FAILURE_CODES.PROMPT_INPUT_NOT_FOUND]: 'Could not find a visible ChatGPT message composer.',
    [FAILURE_CODES.PROMPT_SEND_FAILED]: 'Could not send the one-shot prompt.',
    [FAILURE_CODES.RESPONSE_TIMEOUT]: 'The new assistant response did not complete before the hard timeout.',
    [FAILURE_CODES.RESPONSE_EXTRACTION_FAILED]: 'Could not identify exactly one new assistant response.',
    [FAILURE_CODES.UNEXPECTED_PAGE_STATE]: 'The ChatGPT page is in an unexpected state.',
    [FAILURE_CODES.CONTINUATION_RECEIPT_NOT_FOUND]: 'The continuation receipt was not found.',
    [FAILURE_CODES.CONTINUATION_RECEIPT_INVALID]: 'The continuation receipt is invalid or not bridge-created.',
    [FAILURE_CODES.CONTINUATION_CHAT_NOT_FOUND]: 'The continuation conversation could not be opened.',
    [FAILURE_CODES.FRESH_CHAT_CREATION_FAILED]: 'Could not explicitly create a fresh ChatGPT conversation.',
    [FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH]: 'The ChatGPT conversation identity did not match the requested lineage.',
    [FAILURE_CODES.PROJECT_URL_INVALID]: 'The project URL must be an explicit safe https://chatgpt.com target.',
    [FAILURE_CODES.PROJECT_SCOPE_REQUIRED]: 'A project-scoped consultation cannot use homepage fallback transport.',
    [FAILURE_CODES.PROJECT_SCOPE_MISMATCH]: 'The requested ChatGPT project does not match the receipt project scope.',
    [FAILURE_CODES.PROJECT_NAVIGATION_FAILED]: 'Could not open the requested ChatGPT project page.',
    [FAILURE_CODES.ATTACHMENT_INVALID]: 'The attachment path is invalid.',
    [FAILURE_CODES.ATTACHMENT_NOT_FOUND]: 'The attachment file was not found.',
    [FAILURE_CODES.ATTACHMENT_NOT_REGULAR_FILE]: 'Attachments must be regular files, not directories.',
    [FAILURE_CODES.ATTACHMENT_NOT_READABLE]: 'The attachment file is not readable.',
    [FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION]: 'The attachment is outside the configured allowed roots.',
    [FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED]: 'The attachment path is reserved for authentication or credentials.',
    [FAILURE_CODES.ATTACHMENT_COUNT_EXCEEDED]: `At most ${MAX_ATTACHMENTS} attachments are allowed.`,
    [FAILURE_CODES.ATTACHMENT_SIZE_EXCEEDED]: `Each attachment must be at most ${MAX_ATTACHMENT_BYTES} bytes for this local PoC.`,
    [FAILURE_CODES.ATTACHMENT_UPLOAD_FAILED]: 'ChatGPT rejected or failed to upload an attachment.',
    [FAILURE_CODES.ATTACHMENT_NOT_READY]: 'ChatGPT attachments did not reach a ready state.',
    [FAILURE_CODES.CONTEXT_PACK_INVALID]: 'The context pack contract is invalid.',
    [FAILURE_CODES.CONTEXT_PACK_ALREADY_EXISTS]: 'The context pack packet id already exists.',
    [FAILURE_CODES.CONTEXT_PACK_SOURCE_NOT_FOUND]: 'An explicitly selected context evidence file was not found.',
    [FAILURE_CODES.CONTEXT_PACK_SOURCE_ROOT_VIOLATION]: 'Context evidence is outside the configured evidence roots.',
    [FAILURE_CODES.CONTEXT_PACK_SOURCE_NOT_REGULAR_FILE]: 'Context evidence must be a regular file.',
    [FAILURE_CODES.CONTEXT_PACK_SOURCE_SIZE_EXCEEDED]: 'Context evidence exceeds the bounded packet size.',
    [FAILURE_CODES.CONTEXT_PACK_DUPLICATE_LOGICAL_FILE]: 'A context logical file was selected with conflicting content.',
    [FAILURE_CODES.CONTEXT_PACK_ATTACHMENT_COUNT_EXCEEDED]: 'The context pack contains too many attachments.',
    [FAILURE_CODES.CONTEXT_PACK_SECRET_REJECTED]: 'Secret material was detected in outgoing context and the packet was rejected.',
    [FAILURE_CODES.CONTEXT_PACK_ABSOLUTE_PATH_REJECTED]: 'An absolute local path was detected in GPT-visible context.',
    [FAILURE_CODES.CONTEXT_PACK_MANIFEST_INVALID]: 'The context pack manifest is invalid.',
    [FAILURE_CODES.CONTEXT_PACK_MANIFEST_NOT_FOUND]: 'The context pack manifest or staged file was not found.',
    [FAILURE_CODES.CONTEXT_PACK_INTEGRITY_FAILED]: 'The context pack changed after staging.',
  }[code] || 'The browser bridge failed.';
}

// Keep the established bridge codes stable for existing callers while exposing
// the bounded, user-facing taxonomy required by the Workflow integration.
function failureClassForCode(code) {
  return {
    [FAILURE_CODES.BRIDGE_TIMEOUT]: 'PRE_PROMPT_TRANSIENT_INFRASTRUCTURE_FAILURE',
    [FAILURE_CODES.NETWORK_TRANSIENT]: 'PRE_PROMPT_TRANSIENT_INFRASTRUCTURE_FAILURE',
    [FAILURE_CODES.TARGET_CLOSED]: 'PRE_PROMPT_TRANSIENT_INFRASTRUCTURE_FAILURE',
    [FAILURE_CODES.LOGIN_REQUIRED]: 'GPT_AUTH_REQUIRED',
    [FAILURE_CODES.CHATGPT_NAVIGATION_FAILED]: 'CHATGPT_PAGE_UNREACHABLE',
    [FAILURE_CODES.PROJECT_NAVIGATION_FAILED]: 'PROJECT_SCOPE_NOT_FOUND',
    [FAILURE_CODES.PROJECT_SCOPE_MISMATCH]: 'PROJECT_SCOPE_NOT_FOUND',
    [FAILURE_CODES.PROJECT_SCOPE_REQUIRED]: 'PROJECT_SCOPE_NOT_FOUND',
    [FAILURE_CODES.PROMPT_INPUT_NOT_FOUND]: 'COMPOSER_NOT_READY',
    [FAILURE_CODES.FRESH_CHAT_CREATION_FAILED]: 'FRESH_CHAT_CREATION_FAILED',
    [FAILURE_CODES.ATTACHMENT_NOT_READY]: 'ATTACHMENT_NOT_READY',
    [FAILURE_CODES.PROMPT_SEND_FAILED]: 'PROMPT_SUBMISSION_FAILED',
    [FAILURE_CODES.RESPONSE_TIMEOUT]: 'RESPONSE_TIMEOUT',
    [FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH]: 'CONVERSATION_IDENTITY_MISMATCH',
  }[code] || code;
}

function safeBrowserCheckpoints(value) {
  if (!Array.isArray(value)) return null;
  const result = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const checkpoint = typeof item.checkpoint === 'string' && /^B(?:[0-9]|1[0-2])$/.test(item.checkpoint)
      ? item.checkpoint
      : null;
    const status = typeof item.status === 'string' && Object.values(BROWSER_CHECKPOINT_STATUSES).includes(item.status)
      ? item.status
      : null;
    if (!checkpoint || !status) continue;
    const safe = { checkpoint, status };
    if (typeof item.failure_class === 'string' && /^[A-Z0-9_]{1,64}$/.test(item.failure_class)) {
      safe.failure_class = item.failure_class;
    }
    result.push(safe);
    if (result.length >= 32) break;
  }
  return result.length > 0 ? result : null;
}

export function assertRequestBudget(requestCount) {
  if (!Number.isInteger(requestCount) || requestCount < 0 || requestCount > MAX_CHATGPT_REQUESTS_PER_INVOCATION) {
    throw new BridgeError(
      FAILURE_CODES.UNEXPECTED_PAGE_STATE,
      `The invocation permits at most ${MAX_CHATGPT_REQUESTS_PER_INVOCATION} ChatGPT request.`,
    );
  }
}

function timestampId(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  return [
    date.getUTCFullYear(),
    pad(date.getUTCMonth() + 1),
    pad(date.getUTCDate()),
    '-',
    pad(date.getUTCHours()),
    pad(date.getUTCMinutes()),
    pad(date.getUTCSeconds()),
  ].join('');
}

export function createConsultationId(date = new Date(), random = crypto.randomUUID()) {
  return `CONSULT-${timestampId(date)}-${random.slice(0, 8)}`;
}

function displayProfile(profileDir, rootDir) {
  const relative = path.relative(rootDir, profileDir);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative)
    ? relative.split(path.sep).join('/')
    : profileDir;
}

export function isValidConsultationId(value) {
  return typeof value === 'string' && CONSULTATION_ID_PATTERN.test(value);
}

export function extractConversationIdFromUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.origin !== CHATGPT_ORIGIN) return null;
  const segments = parsed.pathname.split('/').filter(Boolean);
  const globalConversationRoute = segments.length === 2 && segments[0] === 'c';
  const projectConversationRoute = segments.length >= 3
    && segments.at(-2) === 'c';
  if ((!globalConversationRoute && !projectConversationRoute) || !CONVERSATION_ID_PATTERN.test(segments.at(-1))) {
    return null;
  }
  return segments.at(-1);
}

/**
 * Return a same-origin conversation route with query/hash state removed.
 * ChatGPT can briefly expose the new assistant turn before it commits the
 * conversation URL; this lets hydration recovery save a safe route and
 * restore it if a reload falls back to the global root.
 */
function sanitizedConversationRoute(value) {
  const conversationId = extractConversationIdFromUrl(value);
  if (!conversationId) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  return `${parsed.origin}${parsed.pathname}`;
}

export function isValidConversationUrl(value, expectedConversationId) {
  const conversationId = extractConversationIdFromUrl(value);
  return Boolean(conversationId && (
    expectedConversationId === undefined || conversationId === expectedConversationId
  ));
}

export function normalizeTransport(value) {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !Object.values(TRANSPORTS).includes(value)) {
    throw new BridgeError(
      FAILURE_CODES.UNEXPECTED_PAGE_STATE,
      `transport must be one of: ${Object.values(TRANSPORTS).join(', ')}.`,
    );
  }
  return value;
}

function safeTransport(value) {
  try {
    return normalizeTransport(value);
  } catch {
    return null;
  }
}

function isChatGptHomepageUrl(value) {
  if (typeof value !== 'string' || !value) return false;
  try {
    const parsed = new URL(value);
    return parsed.origin === CHATGPT_ORIGIN
      && parsed.pathname === '/'
      && !parsed.search
      && !parsed.hash;
  } catch {
    return false;
  }
}

function projectUrlError(message, cause) {
  return new BridgeError(
    FAILURE_CODES.PROJECT_URL_INVALID,
    message || failureMessage(FAILURE_CODES.PROJECT_URL_INVALID),
    cause,
  );
}

/**
 * Normalize and validate the explicit ChatGPT project URL contract.
 *
 * This validates only the minimum safe target contract. The browser owns the
 * product-specific Project page check, so no Project identifier or route
 * structure is inferred here.
 */
export function normalizeProjectUrl(value) {
  if (typeof value !== 'string' || !value || value.length > PROJECT_URL_MAX_CHARS || value !== value.trim()) {
    throw projectUrlError();
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw projectUrlError(undefined, error);
  }
  if (
    parsed.origin !== CHATGPT_ORIGIN
    || parsed.protocol !== 'https:'
    || parsed.hostname !== 'chatgpt.com'
    || parsed.port
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw projectUrlError();
  }

  // URL parsing normalizes dot segments and escaped characters. Compare the
  // raw path as well so an ambiguous spelling cannot pass as a target URL.
  const authorityEnd = value.indexOf('/', value.indexOf('://') + 3);
  const rawPathAndSuffix = authorityEnd === -1 ? '' : value.slice(authorityEnd);
  const rawPath = rawPathAndSuffix.split(/[?#]/, 1)[0];
  const rawPathWithoutTrailingSlash = rawPath.endsWith('/') ? rawPath.slice(0, -1) : rawPath;
  const parsedPathWithoutTrailingSlash = parsed.pathname.endsWith('/')
    ? parsed.pathname.slice(0, -1)
    : parsed.pathname;
  if (!rawPath || rawPathWithoutTrailingSlash !== parsedPathWithoutTrailingSlash) {
    throw projectUrlError();
  }

  if (
    parsed.pathname === '/'
    || parsed.pathname.endsWith('/.')
    || parsed.pathname.endsWith('/..')
    || parsed.pathname.includes('\\')
    || [...parsed.pathname].some((character) => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) === 0x7f)
  ) {
    throw projectUrlError();
  }
  if (!PROJECT_ROUTE_PATTERN.test(parsed.pathname)) throw projectUrlError();
  return `${CHATGPT_ORIGIN}${parsed.pathname.endsWith('/') ? parsed.pathname.slice(0, -1) : parsed.pathname}`;
}

export function isValidProjectUrl(value) {
  try {
    normalizeProjectUrl(value);
    return true;
  } catch {
    return false;
  }
}

function parseProjectConversationUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.origin !== CHATGPT_ORIGIN || parsed.search || parsed.hash) return null;
  const segments = parsed.pathname.split('/').filter(Boolean);
  if (segments.length < 3 || segments.at(-2) !== 'c' || !CONVERSATION_ID_PATTERN.test(segments.at(-1))) return null;
  return {
    projectSlug: segments.at(-3),
    conversationId: segments.at(-1),
  };
}

/**
 * Validate that a URL is the exact Project conversation route bound to a
 * canonical Project landing URL.  This is intentionally stricter than the
 * general conversation URL parser: a Project-scoped continuation must not
 * silently fall back to a global `/c/<id>` route or another Project slug.
 */
export function isValidProjectConversationUrl(value, projectUrl, expectedConversationId = undefined) {
  let expectedProjectSlug;
  try {
    const normalizedProjectUrl = normalizeProjectUrl(projectUrl);
    const targetPath = new URL(normalizedProjectUrl).pathname;
    const knownTarget = targetPath.match(PROJECT_ROUTE_PATTERN);
    expectedProjectSlug = knownTarget ? knownTarget[1] : null;
  } catch {
    return false;
  }
  const parsed = parseProjectConversationUrl(value);
  if (!parsed || (expectedProjectSlug !== null && parsed.projectSlug !== expectedProjectSlug)) return false;
  return expectedConversationId === undefined || parsed.conversationId === expectedConversationId;
}

const PROJECT_NAVIGATION_FAILURE_CLASS_VALUES = new Set(
  Object.values(PROJECT_NAVIGATION_FAILURE_CLASSES),
);

function boundedNavigationElapsed(startedAt) {
  const elapsed = Date.now() - startedAt;
  if (!Number.isFinite(elapsed)) return MAX_PROJECT_NAVIGATION_ELAPSED_MS;
  return Math.max(0, Math.min(MAX_PROJECT_NAVIGATION_ELAPSED_MS, Math.round(elapsed)));
}

function sanitizeNavigationText(value, maxChars = MAX_PROJECT_NAVIGATION_TITLE_CHARS) {
  if (typeof value !== 'string') return null;
  const sanitized = value
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(
      /\b(?:token|password|cookie|secret|authorization|bearer|session(?:id)?)\s*[:=]\s*[^\s]+/gi,
      (match) => match.replace(/[^\s:=]+$/, '<redacted>'),
    )
    .trim();
  if (!sanitized) return null;
  return sanitized.slice(0, maxChars);
}

/**
 * Keep only a URL's origin and path for diagnostics. Query/hash/credential
 * material is intentionally discarded before a URL can reach a receipt.
 */
export function sanitizeProjectNavigationUrl(value) {
  if (typeof value !== 'string' || !value || value.length > PROJECT_URL_MAX_CHARS) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' || !parsed.origin || !parsed.pathname) return null;
  const pathOnly = parsed.pathname.slice(0, PROJECT_URL_MAX_CHARS);
  const sanitized = `${parsed.origin}${pathOnly}`;
  return sanitized.length <= PROJECT_URL_MAX_CHARS ? sanitized : sanitized.slice(0, PROJECT_URL_MAX_CHARS);
}

export function classifyProjectNavigationError(error) {
  const name = typeof error?.name === 'string' ? error.name.toLowerCase() : '';
  const message = typeof error?.message === 'string' ? error.message.toLowerCase() : '';
  const code = typeof error?.code === 'string' ? error.code.toLowerCase() : '';
  const combined = `${name} ${message} ${code}`;
  if (error?.failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP
      || combined.includes('project_frontend_bootstrap_failure')) {
    return PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP;
  }
  if (
    name.includes('targetclosed')
    || /target(?: page| context)?[^\n]*closed|page[^\n]*closed|context[^\n]*closed|browser[^\n]*closed|has been closed/.test(combined)
  ) {
    return PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED;
  }
  if (name.includes('timeout') || /timeout|timed out|timedout|etimedout|deadline exceeded/.test(combined)) {
    return PROJECT_NAVIGATION_FAILURE_CLASSES.TIMEOUT;
  }
  if (/http[^\n]*(?:status|response)[^\n]*(?:code|4\d\d|5\d\d)|response[^\n]*(?:status|code)[=: ]+(?:4\d\d|5\d\d)|err_http_response_code/.test(combined)) {
    return PROJECT_NAVIGATION_FAILURE_CLASSES.HTTP_ERROR;
  }
  if (
    /net::err_|err_(?:connection|internet|name_not_resolved|network|address)|ns_error|network|connection reset|socket|dns|failed to fetch|disconnected/.test(combined)
  ) {
    return PROJECT_NAVIGATION_FAILURE_CLASSES.NETWORK;
  }
  return PROJECT_NAVIGATION_FAILURE_CLASSES.UNKNOWN;
}

export function isRetryableProjectNavigationError(error) {
  const failureClass = classifyProjectNavigationError(error);
  return failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP
    || failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.TIMEOUT
    || failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.NETWORK
    || failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED;
}

function safeNavigationStatus(response) {
  if (!response) return null;
  let status;
  try {
    status = typeof response.status === 'function' ? response.status() : response.status;
  } catch {
    return null;
  }
  return Number.isInteger(status) && status >= 0 && status <= 999 ? status : null;
}

function safePageUrl(page) {
  try {
    const value = typeof page?.url === 'function' ? page.url() : page?.url;
    return typeof value === 'string' ? value : null;
  } catch {
    return null;
  }
}

function processHasExited(child) {
  return (child?.exitCode !== null && child?.exitCode !== undefined)
    || (child?.signalCode !== null && child?.signalCode !== undefined);
}

/**
 * Wait for an owned child process to report exit/close, without keeping a
 * detached bridge process alive indefinitely.  The caller remains responsible
 * for the bounded kill fallback when this returns false.
 */
function waitForChildProcessExit(child, timeoutMs) {
  if (!child || processHasExited(child)) return Promise.resolve(true);
  if (typeof child.once !== 'function') return Promise.resolve(false);
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (exited) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (typeof child.removeListener === 'function') {
        child.removeListener('exit', onExit);
        child.removeListener('close', onClose);
      }
      resolve(exited);
    };
    const onExit = () => finish(true);
    const onClose = () => finish(true);
    child.once('exit', onExit);
    child.once('close', onClose);
    timer = setTimeout(() => finish(processHasExited(child)), timeoutMs);
  });
}

async function safePageTitle(page) {
  try {
    const value = typeof page?.title === 'function' ? await page.title() : page?.title;
    return sanitizeNavigationText(value);
  } catch {
    return null;
  }
}

function navigationErrorHash(error) {
  const message = typeof error?.message === 'string' ? error.message : String(error || 'unknown');
  return crypto.createHash('sha256').update(message.slice(0, 1024), 'utf8').digest('hex');
}

/**
 * Receipt-safe project navigation diagnostics. Keep this allow-list narrow:
 * it may contain status/timing and bounded presentation metadata, never page
 * content, cookies, tokens, or raw browser errors.
 */
export function safeProjectNavigationDiagnostics(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const safe = {};
  for (const field of ['attempt_count', 'retry_count', 'elapsed_ms']) {
    if (Number.isInteger(value[field]) && value[field] >= 0) {
      const cap = field === 'elapsed_ms'
        ? MAX_PROJECT_NAVIGATION_ELAPSED_MS
        : field === 'retry_count' ? MAX_PROJECT_NAVIGATION_RETRIES : MAX_PROJECT_NAVIGATION_RETRIES + 1;
      safe[field] = Math.min(cap, value[field]);
    }
  }
  const status = value.http_status === undefined ? value.status : value.http_status;
  if (Number.isInteger(status) && status >= 0 && status <= 999) safe.http_status = status;
  else if (status === null) safe.http_status = null;
  for (const field of ['title']) {
    const sanitized = sanitizeNavigationText(value[field]);
    if (sanitized) safe[field] = sanitized;
    else if (value[field] === null) safe[field] = null;
  }
  for (const field of ['landing_url', 'requested_url']) {
    const sanitized = sanitizeProjectNavigationUrl(value[field]);
    if (sanitized) safe[field] = sanitized;
    else if (value[field] === null) safe[field] = null;
  }
  if (typeof value.bootstrap_used === 'boolean') safe.bootstrap_used = value.bootstrap_used;
  const bootstrapUrl = sanitizeProjectNavigationUrl(value.bootstrap_url);
  if (bootstrapUrl) safe.bootstrap_url = bootstrapUrl;
  else if (value.bootstrap_url === null) safe.bootstrap_url = null;
  if (PROJECT_NAVIGATION_FAILURE_CLASS_VALUES.has(value.failure_class)) {
    safe.failure_class = value.failure_class;
  }
  if (typeof value.error_hash === 'string' && /^[0-9a-f]{64}$/i.test(value.error_hash)) {
    safe.error_hash = value.error_hash.toLowerCase();
  } else if (value.error_hash === null) {
    safe.error_hash = null;
  }
  return Object.keys(safe).length > 0 ? safe : null;
}

function projectNavigationDiagnosticsForAttempt({
  requestedUrl,
  attemptCount,
  retryCount,
  startedAt,
  status = null,
  title = null,
  landingUrl = null,
  failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.NONE,
  error = null,
  bootstrapUsed = false,
  bootstrapUrl = null,
}) {
  const diagnostics = {
    requested_url: requestedUrl,
    attempt_count: attemptCount,
    retry_count: retryCount,
    elapsed_ms: boundedNavigationElapsed(startedAt),
    http_status: status,
    title,
    landing_url: landingUrl,
    failure_class: failureClass,
    error_hash: error ? navigationErrorHash(error) : null,
  };
  // Keep the legacy lightweight page doubles backward-compatible while
  // recording bootstrap provenance for real Playwright navigations.
  if (bootstrapUsed === true) {
    diagnostics.bootstrap_used = true;
    diagnostics.bootstrap_url = bootstrapUrl;
  }
  return safeProjectNavigationDiagnostics(diagnostics);
}

function resolveProjectUrlAlias({ projectUrl, project_url } = {}) {
  if (projectUrl !== undefined && project_url !== undefined) {
    const normalizedCamel = normalizeProjectUrl(projectUrl);
    const normalizedSnake = normalizeProjectUrl(project_url);
    if (normalizedCamel !== normalizedSnake) {
      throw new BridgeError(
        FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
        failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
      );
    }
    return normalizedCamel;
  }
  if (projectUrl !== undefined) return normalizeProjectUrl(projectUrl);
  if (project_url !== undefined) return normalizeProjectUrl(project_url);
  return undefined;
}

function safeProjectScopeUrl(value) {
  if (value === undefined || value === null) return null;
  try {
    return normalizeProjectUrl(value);
  } catch {
    return null;
  }
}

function safeProjectScopeEvidence(value, projectUrl) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const safe = {};
  const initial = value.initial_navigation;
  if (initial && typeof initial === 'object' && !Array.isArray(initial)) {
    const initialSafe = {};
    for (const field of ['matched', 'verified']) {
      if (typeof initial[field] === 'boolean') initialSafe[field] = initial[field];
    }
    for (const field of ['requested_url', 'landed_url']) {
      const normalized = safeProjectScopeUrl(initial[field]);
      if (normalized && (!projectUrl || normalized === projectUrl)) initialSafe[field] = normalized;
      else if (initial[field] === null) initialSafe[field] = null;
    }
    if (Object.keys(initialSafe).length > 0) safe.initial_navigation = initialSafe;
  }
  const binding = value.parent_binding;
  if (binding && typeof binding === 'object' && !Array.isArray(binding)) {
    const bindingSafe = {};
    for (const field of ['matched', 'verified']) {
      if (typeof binding[field] === 'boolean') bindingSafe[field] = binding[field];
    }
    if (Object.keys(bindingSafe).length > 0) safe.parent_binding = bindingSafe;
  }
  return Object.keys(safe).length > 0 ? safe : null;
}

function hasProjectScopeFields(receipt) {
  return [
    'project_url',
    'project_scope_requested',
    'project_scope_verified',
    'project_scope_evidence',
  ].some((field) => Object.prototype.hasOwnProperty.call(receipt, field));
}

function hasChatgptTargetFields(receipt) {
  return [
    'chatgpt_target_mode',
    'chatgpt_target_url_digest',
    'chatgpt_target_origin',
    'chatgpt_project_target_verified',
    'fresh_project_chat_created',
  ].some((field) => Object.prototype.hasOwnProperty.call(receipt, field));
}

/** Validate the bounded target markers emitted for new bridge receipts. */
export function validateChatgptTargetMetadata(receipt) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) return false;
  if (!hasChatgptTargetFields(receipt)) return true;
  const fields = [
    'chatgpt_target_mode',
    'chatgpt_target_url_digest',
    'chatgpt_target_origin',
    'chatgpt_project_target_verified',
    'fresh_project_chat_created',
  ];
  if (!fields.every((field) => Object.prototype.hasOwnProperty.call(receipt, field))) return false;
  if (!['PROJECT', 'DEFAULT'].includes(receipt.chatgpt_target_mode)) return false;
  if (receipt.chatgpt_target_origin !== CHATGPT_ORIGIN) return false;
  if (!['YES', 'NO'].includes(receipt.chatgpt_project_target_verified)) return false;
  if (!['YES', 'NO'].includes(receipt.fresh_project_chat_created)) return false;
  const projectUrl = safeProjectScopeUrl(receipt.project_url);
  if (receipt.chatgpt_target_mode === 'PROJECT') {
    if (!projectUrl || receipt.project_url !== projectUrl) return false;
    if (typeof receipt.chatgpt_target_url_digest !== 'string' || !/^[0-9a-f]{64}$/.test(receipt.chatgpt_target_url_digest)) return false;
    const expectedDigest = crypto.createHash('sha256').update(projectUrl, 'utf8').digest('hex');
    if (receipt.chatgpt_target_url_digest !== expectedDigest) return false;
    if (receipt.chatgpt_project_target_verified !== 'YES') return false;
    if (receipt.fresh_project_chat_created === 'YES' && (
      receipt.mode !== CONVERSATION_MODES.FRESH
      || receipt.status !== 'complete'
      || receipt.conversation_validated !== true
      || !isValidConversationUrl(receipt.chat_url, receipt.conversation_id)
    )) return false;
  } else if (
    receipt.chatgpt_target_url_digest !== null
    || projectUrl !== null
    || receipt.chatgpt_project_target_verified !== 'NO'
    || receipt.fresh_project_chat_created !== 'NO'
  ) return false;
  return true;
}

/** Validate optional receipt project-scope metadata without requiring it for legacy receipts. */
export function validateProjectScopeMetadata(receipt, { requireVerified = false } = {}) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) return false;
  if (!hasProjectScopeFields(receipt)) return true;
  const projectUrl = safeProjectScopeUrl(receipt.project_url);
  if (!projectUrl || receipt.project_url !== projectUrl) return false;
  if (receipt.project_scope_requested !== true) return false;
  if (typeof receipt.project_scope_verified !== 'boolean') return false;
  const evidence = safeProjectScopeEvidence(receipt.project_scope_evidence, projectUrl);
  if (!evidence || !evidence.initial_navigation) return false;
  const initial = evidence.initial_navigation;
  if (typeof initial.matched !== 'boolean' || typeof initial.verified !== 'boolean') return false;
  if (initial.requested_url !== projectUrl) return false;
  if (receipt.project_scope_verified !== (initial.matched === true && initial.verified === true)) return false;
  if (receipt.project_scope_verified && initial.landed_url !== projectUrl) return false;
  if (requireVerified && receipt.project_scope_verified !== true) return false;
  return true;
}

function projectScopeEvidenceForLanding(projectUrl, landedUrl) {
  const normalizedLanded = safeProjectScopeUrl(landedUrl);
  const matched = normalizedLanded === projectUrl;
  return {
    initial_navigation: {
      requested_url: projectUrl,
      landed_url: normalizedLanded,
      matched,
      verified: matched,
    },
  };
}

function projectScopeEvidenceForBinding(projectUrl, matched) {
  return {
    initial_navigation: {
      requested_url: projectUrl,
      landed_url: null,
      matched: false,
      verified: false,
    },
    parent_binding: { matched, verified: matched },
  };
}

function projectScopeEvidenceForContinuation(projectUrl, parentEvidence) {
  const safeParentEvidence = safeProjectScopeEvidence(parentEvidence, projectUrl);
  const initial = safeParentEvidence?.initial_navigation;
  if (
    !initial
    || initial.requested_url !== projectUrl
    || initial.landed_url !== projectUrl
    || initial.matched !== true
    || initial.verified !== true
  ) {
    return null;
  }
  return {
    initial_navigation: { ...initial },
    parent_binding: { matched: true, verified: true },
  };
}

export function validateContinuationReceipt(receipt, expectedConsultationId) {
  if (!receipt || typeof receipt !== 'object') return false;
  if (!isValidConsultationId(expectedConsultationId) || receipt.consultation_id !== expectedConsultationId) {
    return false;
  }
  if (!isValidConsultationId(receipt.consultation_id)) return false;
  if (!Object.values(CONVERSATION_MODES).includes(receipt.mode)) return false;
  if (receipt.status !== 'complete' || receipt.request_count !== MAX_CHATGPT_REQUESTS_PER_INVOCATION) {
    return false;
  }
  if (typeof receipt.created_at !== 'string' || !receipt.created_at) return false;
  if (typeof receipt.profile !== 'string' || !receipt.profile) return false;
  if (!Number.isInteger(receipt.response_char_count) || receipt.response_char_count < 0) return false;
  if (!validateReceiptAttachments(receipt.attachments, { complete: true })) return false;
  if (receipt.context_pack !== undefined && !validateContextPackReceiptMetadata(receipt.context_pack)) return false;
  if (!validateChatgptTargetMetadata(receipt)) return false;
  const receiptTransport = safeTransport(receipt.transport);
  if (receipt.transport !== undefined && receiptTransport === null) return false;
  if (receiptTransport === TRANSPORTS.HOMEPAGE_FALLBACK) {
    // A homepage fallback records the requested Project only as context. It
    // must never carry a project scope claim that a continuation could trust.
    if (hasProjectScopeFields(receipt) || Object.hasOwn(receipt, 'project_url')) return false;
    if (receipt.requested_project_url !== undefined) {
      const requested = safeProjectScopeUrl(receipt.requested_project_url);
      if (!requested || requested !== receipt.requested_project_url) return false;
    }
  } else if (Object.hasOwn(receipt, 'requested_project_url')) {
    return false;
  }
  if (
    receipt.project_navigation_diagnostics !== undefined
    && !safeProjectNavigationDiagnostics(receipt.project_navigation_diagnostics)
  ) return false;
  if (!validateProjectScopeMetadata(receipt, { requireVerified: hasProjectScopeFields(receipt) })) return false;
  if (receipt.conversation_validated !== true) return false;
  if (!isValidConsultationId(receipt.conversation_root_consultation_id)) return false;
  if (!isValidConversationUrl(receipt.chat_url, receipt.conversation_id)) return false;
  if (receipt.mode === CONVERSATION_MODES.FRESH) {
    if (receipt.parent_consultation_id !== null) return false;
  } else if (!isValidConsultationId(receipt.parent_consultation_id)) {
    return false;
  }
  return true;
}

export function deriveContinuationLineage(parentReceipt) {
  if (!validateContinuationReceipt(parentReceipt, parentReceipt?.consultation_id)) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt is invalid or not bridge-created.',
    );
  }
  return {
    parentConsultationId: parentReceipt.consultation_id,
    conversationRootConsultationId: parentReceipt.conversation_root_consultation_id,
    conversationId: parentReceipt.conversation_id,
  };
}

function isPathInside(root, target) {
  const relative = path.relative(root, target);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function isPathWithin(root, target) {
  return path.resolve(root) === path.resolve(target) || Boolean(isPathInside(root, target));
}

function isSafeReceiptRelativePath(value) {
  if (typeof value !== 'string' || !value || value.includes('\0')) return false;
  // Receipts are portable metadata. Reject both POSIX and Windows absolute
  // paths even when this process happens to run on the other platform.
  if (
    path.posix.isAbsolute(value)
    || path.win32.isAbsolute(value)
    || value.startsWith('/')
    || value.startsWith('\\')
    || /^[A-Za-z]:/.test(value)
  ) return false;
  return !value.split(/[\\/]/).some((segment) => segment === '..');
}

export function validateReceiptAttachments(attachments, { complete = false } = {}) {
  if (attachments === undefined) return true;
  if (!Array.isArray(attachments) || attachments.length > MAX_ATTACHMENTS) return false;
  return attachments.every((item) => {
    if (!item || typeof item !== 'object') return false;
    if (
      typeof item.basename !== 'string'
      || !item.basename
      || path.basename(item.basename) !== item.basename
      || item.basename.includes('\\')
    ) return false;
    if (!isSafeReceiptRelativePath(item.relative_path)) return false;
    if (!Number.isInteger(item.byte_size) || item.byte_size < 0 || item.byte_size > MAX_ATTACHMENT_BYTES) return false;
    if (typeof item.sha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(item.sha256)) return false;
    if (item.media_type !== null && typeof item.media_type !== 'string') return false;
    if (typeof item.upload_status !== 'string') return false;
    if (complete && item.upload_status !== 'ready') return false;
    return true;
  });
}

const ATTACHMENT_DIAGNOSTIC_BOOLEAN_FIELDS = Object.freeze([
  'file_input_present',
  'file_input_accepted',
  'file_input_multiple',
  'file_input_basenames_matched',
  'expected_basenames_matched',
  'progress_present',
  'progress_completed',
  'error_ui',
  'composer_ready',
  'send_control_present',
  'send_enabled',
  'read_reliable',
  'final_predicate',
  'reattach_attempted',
  'reattach_succeeded',
]);
const ATTACHMENT_DIAGNOSTIC_NUMBER_FIELDS = Object.freeze([
  'requested_count',
  'file_input_count',
  'file_input_total_count',
  'file_input_selected_index',
  'composer_scoped_input_count',
  'file_input_basename_count',
  'visible_chip_count',
  'visible_attachment_candidate_count',
  'visible_attachment_semantic_count',
  'visible_attachment_contradiction_count',
  'expected_basename_count',
  'matched_basename_count',
  'pending_count',
  'error_count',
  'elapsed_wait_ms',
  'reattach_attempt_count',
  'reattach_elapsed_ms',
]);
const ATTACHMENT_DIAGNOSTIC_BASENAME_FIELDS = Object.freeze([
  'expected_basenames',
  'file_input_basenames',
  'tile_basenames',
  'matched_basenames',
]);

function safeAttachmentDiagnostics(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const safe = {};
  for (const field of ATTACHMENT_DIAGNOSTIC_BOOLEAN_FIELDS) {
    if (typeof value[field] === 'boolean') safe[field] = value[field];
  }
  for (const field of ATTACHMENT_DIAGNOSTIC_NUMBER_FIELDS) {
    if (Number.isInteger(value[field]) && value[field] >= 0) safe[field] = value[field];
  }
  for (const field of ATTACHMENT_DIAGNOSTIC_BASENAME_FIELDS) {
    if (!Array.isArray(value[field])) continue;
    const names = [];
    const seen = new Set();
    for (const candidate of value[field]) {
      if (typeof candidate !== 'string') continue;
      const name = path.basename(candidate).trim();
      if (!name || name.length > 128 || name.includes('\\') || name.includes('/') || seen.has(name)) continue;
      seen.add(name);
      names.push(name);
      if (names.length >= 16) break;
    }
    safe[field] = names;
  }
  if (['none', 'visible_wrong_attachment_basename'].includes(value.attachment_contradiction_reason)) {
    safe.attachment_contradiction_reason = value.attachment_contradiction_reason;
  }
  if (typeof value.file_input_owner === 'string' && ['unified-composer', 'global-fallback', 'none'].includes(value.file_input_owner)) {
    safe.file_input_owner = value.file_input_owner;
  }
  if (typeof value.file_input_accept === 'string' && /^[a-z0-9.+*\/-]{1,128}(?:,[a-z0-9.+*\/-]{1,128})*$/.test(value.file_input_accept)) {
    safe.file_input_accept = value.file_input_accept;
  } else if (value.file_input_accept === null) {
    safe.file_input_accept = null;
  }
  if (typeof value.mode === 'string' && /^(?:fresh|continue|normal)$/.test(value.mode)) safe.mode = value.mode;
  if (Number.isInteger(value.request_count) && value.request_count >= 0 && value.request_count <= MAX_CHATGPT_REQUESTS_PER_INVOCATION) {
    safe.request_count = value.request_count;
  }
  return Object.keys(safe).length > 0 ? safe : null;
}

const RESPONSE_FORENSIC_BOOLEAN_FIELDS = Object.freeze([
  'rate_limit_banner_detected',
  'too_many_requests_detected',
  'generic_error_banner_detected',
  'retry_button_detected',
  'generation_stopped_detected',
  'user_turn_submitted',
  'stop_button_present',
  'generation_active',
  'streaming_seen',
  'completion_seen',
  'completion_predicate',
  'empty_assistant_refresh_attempted',
  'empty_assistant_refresh_succeeded',
]);
const RESPONSE_FORENSIC_NUMBER_FIELDS = Object.freeze([
  'baseline_assistant_count',
  'visible_assistant_count',
  'visible_assistant_dom_node_count',
  'unique_logical_slot_count',
  'new_assistant_count',
  't_send_started_ms',
  't_send_completed_ms',
  't_generation_indicator_first_seen_ms',
  't_first_new_visible_assistant_slot_ms',
  't_first_nonempty_assistant_text_ms',
  't_generation_indicator_gone_ms',
  't_response_text_stable_ms',
  't_completion_predicate_true_ms',
  't_extractor_success_ms',
  't_main_deadline_expired_ms',
  't_empty_assistant_refresh_ms',
  'latest_text_length',
  'latest_user_turn_text_length',
  'submitted_user_turn_text_length',
  'elapsed_ms',
  'deadline_ms',
  'observation_count',
]);
const RESPONSE_FORENSIC_HASH_FIELDS = Object.freeze([
  'latest_user_turn_id_hash',
  'latest_user_turn_slot_hash',
  'latest_user_turn_text_hash',
  'submitted_user_turn_id_hash',
  'submitted_user_turn_slot_hash',
  'submitted_user_turn_text_hash',
  'first_new_message_id_hash',
  'latest_message_id_hash',
  'latest_text_sha256',
]);
const RESPONSE_FORENSIC_HASH_LIST_FIELDS = Object.freeze([
  'baseline_assistant_id_hashes',
  'baseline_logical_slot_hashes',
  'duplicate_logical_slot_hashes',
]);
const RESPONSE_FORENSIC_REJECT_INVARIANTS = new Set([
  'response_deadline',
  'snapshot_identity_conflict',
  'multiple_new_assistant_slots',
  'multiple_new_assistant_messages',
  'generation_state_read_failed',
  'response_snapshot_or_extractor_error',
]);
const RESPONSE_FORENSIC_REFRESH_TRIGGERS = new Set([
  'active_generation',
  'stopped_generation',
]);
const RESPONSE_FORENSIC_CLASSES = new Set(Object.values(RESPONSE_FAILURE_CLASSES));

function safeForensicHash(value) {
  return typeof value === 'string' && /^(?:[0-9a-f]{16}|[0-9a-f]{64})$/i.test(value) ? value.toLowerCase() : null;
}

function safeForensicHashList(value) {
  if (!Array.isArray(value)) return [];
  const result = [];
  const seen = new Set();
  for (const item of value) {
    const hash = safeForensicHash(item);
    if (!hash || seen.has(hash)) continue;
    seen.add(hash);
    result.push(hash);
    if (result.length >= 16) break;
  }
  return result;
}

function safeResponseForensic(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const safe = {};
  for (const field of RESPONSE_FORENSIC_BOOLEAN_FIELDS) {
    if (typeof value[field] === 'boolean') safe[field] = value[field];
  }
  for (const field of RESPONSE_FORENSIC_NUMBER_FIELDS) {
    if (Number.isInteger(value[field]) && value[field] >= 0) safe[field] = value[field];
  }
  for (const field of RESPONSE_FORENSIC_HASH_FIELDS) {
    const hash = safeForensicHash(value[field]);
    if (hash) safe[field] = hash;
    else if (value[field] === null) safe[field] = null;
  }
  for (const field of RESPONSE_FORENSIC_HASH_LIST_FIELDS) {
    safe[field] = safeForensicHashList(value[field]);
  }
  if (typeof value.response_failure_class === 'string' && RESPONSE_FORENSIC_CLASSES.has(value.response_failure_class)) {
    safe.response_failure_class = value.response_failure_class;
  }
  if (typeof value.reject_invariant === 'string' && RESPONSE_FORENSIC_REJECT_INVARIANTS.has(value.reject_invariant)) {
    safe.reject_invariant = value.reject_invariant;
  }
  if (typeof value.empty_assistant_refresh_trigger === 'string' && RESPONSE_FORENSIC_REFRESH_TRIGGERS.has(value.empty_assistant_refresh_trigger)) {
    safe.empty_assistant_refresh_trigger = value.empty_assistant_refresh_trigger;
  }
  if (Array.isArray(value.candidates)) {
    safe.candidates = value.candidates.slice(0, 16).map((candidate) => {
      if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return null;
      const output = {};
      for (const field of ['visible', 'is_new', 'streaming', 'completion']) {
        if (typeof candidate[field] === 'boolean') output[field] = candidate[field];
        else if (candidate[field] === null) output[field] = null;
      }
      for (const field of ['logical_slot_hash', 'message_id_hash', 'text_sha256']) {
        const hash = safeForensicHash(candidate[field]);
        if (hash) output[field] = hash;
        else if (candidate[field] === null) output[field] = null;
      }
      if (Number.isInteger(candidate.text_length) && candidate.text_length >= 0) output.text_length = candidate.text_length;
      return Object.keys(output).length > 0 ? output : null;
    }).filter(Boolean);
  }
  return Object.keys(safe).length > 0 ? safe : null;
}

function validateContextPackReceiptMetadata(value) {
  if (!value || typeof value !== 'object') return false;
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.packet_id)) return false;
  if (!['normal', 'fresh'].includes(value.mode)) return false;
  if (
    typeof value.relative_manifest_path !== 'string'
    || !value.relative_manifest_path.startsWith('staging/')
    || value.relative_manifest_path.includes('..')
    || path.posix.isAbsolute(value.relative_manifest_path)
    || path.win32.isAbsolute(value.relative_manifest_path)
  ) return false;
  if (typeof value.manifest_sha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(value.manifest_sha256)) return false;
  if (typeof value.pack_sha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(value.pack_sha256)) return false;
  return Number.isInteger(value.attachment_count) && value.attachment_count >= 0 && value.attachment_count <= MAX_ATTACHMENTS;
}

const ATTACHMENT_MEDIA_TYPES = Object.freeze({
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.csv': 'text/csv',
  '.json': 'application/json',
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml',
});

const SENSITIVE_ATTACHMENT_COMPONENTS = new Set([
  '.auth',
  'chatgpt-profile',
  'browser-user-data-dir',
  'user-data-dir',
  'storage-state',
  'storage_state',
  'cookies',
  'cookie',
]);

function safeAttachmentBasename(value) {
  if (typeof value !== 'string' || !value.trim()) return '<invalid>';
  const basename = value.trim().replace(/^.*[\\/]/, '');
  return basename || '<invalid>';
}

function attachmentMediaType(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  return ATTACHMENT_MEDIA_TYPES[extension] || null;
}

function isSensitiveAttachmentPath(filePath) {
  const normalized = path.normalize(filePath);
  const components = normalized.split(/[\\/]+/).filter(Boolean);
  const lowerComponents = components.map((component) => component.toLowerCase());
  if (lowerComponents.some((component) => SENSITIVE_ATTACHMENT_COMPONENTS.has(component))) return true;
  const basename = lowerComponents.at(-1) || '';
  return (
    /^\.env(?:\..*)?$/.test(basename)
    || /\.(?:pem|key|p12|pfx)$/i.test(basename)
    || /^id_(?:rsa|ed25519)$/.test(basename)
    || /(?:private[-_ ]?key|credential|secret)/i.test(basename)
  );
}

async function resolveProfileBoundary(profileDir, baseDir) {
  if (profileDir === undefined) return null;
  if (typeof profileDir !== 'string' || !profileDir.trim()) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      'The browser profile boundary is invalid for attachment validation.',
    );
  }
  const resolvedProfile = path.resolve(baseDir, profileDir);
  try {
    return await fs.realpath(resolvedProfile);
  } catch (error) {
    // consultOnce validates attachments before opening Chromium, so a new
    // profile directory may not exist yet. Its resolved path is still a
    // binding security boundary; an existing profile is canonicalized above.
    if (error?.code === 'ENOENT') return resolvedProfile;
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      'The browser profile boundary could not be verified for attachment validation.',
      error,
    );
  }
}

function attachmentPlaceholder(value, status = 'pending') {
  return {
    basename: safeAttachmentBasename(value),
    relative_path: null,
    byte_size: null,
    sha256: null,
    media_type: typeof value === 'string' ? attachmentMediaType(value) : null,
    upload_status: status,
  };
}

function attachmentMetadata(file, uploadStatus = 'pending') {
  return {
    basename: file.basename,
    relative_path: file.relativePath,
    byte_size: file.byteSize,
    sha256: file.sha256,
    media_type: file.mediaType,
    upload_status: uploadStatus,
  };
}

function pathError(code, message, cause) {
  return new BridgeError(code, message, cause);
}

export async function resolveAllowedAttachmentRoots({
  allowedAttachmentRoots,
  env = process.env[ATTACHMENT_ROOTS_ENV],
} = {}) {
  let configured = allowedAttachmentRoots;
  if (configured === undefined) {
    configured = typeof env === 'string' && env.trim()
      ? env.split(path.delimiter).map((value) => value.trim()).filter(Boolean)
      : DEFAULT_ALLOWED_ATTACHMENT_ROOTS;
  }
  if (!Array.isArray(configured) || configured.length === 0) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
      'At least one allowed attachment root must be configured.',
    );
  }

  const roots = [];
  for (const candidate of configured) {
    if (typeof candidate !== 'string' || !candidate.trim()) {
      throw pathError(
        FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
        'Allowed attachment roots must be non-empty paths.',
      );
    }
    const resolved = path.resolve(candidate);
    let realRoot;
    try {
      realRoot = await fs.realpath(resolved);
      const rootStat = await fs.stat(realRoot);
      if (!rootStat.isDirectory()) throw new Error('not a directory');
    } catch (error) {
      throw pathError(
        FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
        'An allowed attachment root could not be verified.',
        error,
      );
    }
    if (!roots.some((root) => path.resolve(root) === path.resolve(realRoot))) roots.push(realRoot);
  }
  return roots;
}

export async function validateAttachmentPath(
  attachmentPath,
  {
    allowedAttachmentRoots,
    baseDir = BRIDGE_ROOT,
    profileDir,
    maxBytes = MAX_ATTACHMENT_BYTES,
  } = {},
) {
  if (typeof attachmentPath !== 'string' || !attachmentPath.trim()) {
    throw pathError(FAILURE_CODES.ATTACHMENT_INVALID, 'Attachment paths must be non-empty strings.');
  }
  if (!Array.isArray(allowedAttachmentRoots) || allowedAttachmentRoots.length === 0) {
    throw pathError(FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION, 'Allowed attachment roots were not configured.');
  }
  const requestedPath = path.resolve(baseDir, attachmentPath);
  const profileBoundary = await resolveProfileBoundary(profileDir, baseDir);
  if (profileBoundary && isPathWithin(profileBoundary, requestedPath)) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is inside the browser profile boundary.`,
    );
  }
  if (isSensitiveAttachmentPath(requestedPath)) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is reserved for authentication or credentials.`,
    );
  }

  let realPath;
  try {
    realPath = await fs.realpath(requestedPath);
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw pathError(
        FAILURE_CODES.ATTACHMENT_NOT_FOUND,
        `Attachment ${safeAttachmentBasename(attachmentPath)} was not found.`,
        error,
      );
    }
    throw pathError(
      FAILURE_CODES.ATTACHMENT_INVALID,
      `Attachment ${safeAttachmentBasename(attachmentPath)} could not be canonicalized.`,
      error,
    );
  }
  if (profileBoundary && isPathWithin(profileBoundary, realPath)) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} resolves inside the browser profile boundary.`,
    );
  }
  if (isSensitiveAttachmentPath(realPath)) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SENSITIVE_PATH_DENIED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is reserved for authentication or credentials.`,
    );
  }

  const containingRoot = allowedAttachmentRoots.find((root) => isPathInside(root, realPath));
  if (!containingRoot) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is outside the configured allowed roots.`,
    );
  }

  let fileStat;
  try {
    fileStat = await fs.stat(realPath);
  } catch (error) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_INVALID,
      `Attachment ${safeAttachmentBasename(attachmentPath)} could not be inspected.`,
      error,
    );
  }
  if (!fileStat.isFile()) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_NOT_REGULAR_FILE,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is not a regular file.`,
    );
  }
  if (!Number.isInteger(maxBytes) || maxBytes <= 0 || fileStat.size > maxBytes) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SIZE_EXCEEDED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} exceeds the local bridge safety cap.`,
    );
  }
  try {
    await fs.access(realPath, fsConstants.R_OK);
  } catch (error) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_NOT_READABLE,
      `Attachment ${safeAttachmentBasename(attachmentPath)} is not readable.`,
      error,
    );
  }

  let contents;
  try {
    contents = await fs.readFile(realPath);
  } catch (error) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_NOT_READABLE,
      `Attachment ${safeAttachmentBasename(attachmentPath)} could not be read.`,
      error,
    );
  }
  if (contents.byteLength > maxBytes) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_SIZE_EXCEEDED,
      `Attachment ${safeAttachmentBasename(attachmentPath)} exceeds the local bridge safety cap.`,
    );
  }
  const finalStat = await fs.stat(realPath);
  if (finalStat.size !== fileStat.size || finalStat.mtimeMs !== fileStat.mtimeMs) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_INVALID,
      `Attachment ${safeAttachmentBasename(attachmentPath)} changed while it was being prepared.`,
    );
  }

  const relativePath = path.relative(containingRoot, realPath);
  return {
    realPath,
    basename: path.basename(realPath),
    relativePath: relativePath.split(path.sep).join('/'),
    byteSize: contents.byteLength,
    sha256: crypto.createHash('sha256').update(contents).digest('hex'),
    mediaType: attachmentMediaType(realPath),
  };
}

export async function prepareAttachments(
  attachments,
  {
    allowedAttachmentRoots,
    baseDir = BRIDGE_ROOT,
    profileDir,
    maxBytes = MAX_ATTACHMENT_BYTES,
  } = {},
) {
  if (attachments === undefined) return { files: [], metadata: [] };
  if (!Array.isArray(attachments)) {
    throw pathError(FAILURE_CODES.ATTACHMENT_INVALID, 'attachments must be an array of local paths.');
  }
  if (attachments.length > MAX_ATTACHMENTS) {
    throw pathError(
      FAILURE_CODES.ATTACHMENT_COUNT_EXCEEDED,
      `At most ${MAX_ATTACHMENTS} attachments may be supplied.`,
    );
  }
  if (attachments.length === 0) return { files: [], metadata: [] };
  const roots = allowedAttachmentRoots || await resolveAllowedAttachmentRoots();
  const metadata = attachments.map((item) => attachmentPlaceholder(item));
  const files = [];
  try {
    for (let index = 0; index < attachments.length; index += 1) {
      const file = await validateAttachmentPath(attachments[index], {
        allowedAttachmentRoots: roots,
        baseDir,
        profileDir,
        maxBytes,
      });
      files.push(file);
      metadata[index] = attachmentMetadata(file);
    }
  } catch (error) {
    const failure = asBridgeError(error, FAILURE_CODES.ATTACHMENT_INVALID);
    const failedIndex = files.length;
    if (metadata[failedIndex]) metadata[failedIndex].upload_status = 'rejected';
    for (let index = failedIndex + 1; index < metadata.length; index += 1) {
      metadata[index].upload_status = 'not_attempted';
    }
    failure.attachmentMetadata = metadata;
    throw failure;
  }
  return { files, metadata };
}

export async function readContinuationReceipt({ rootDir, consultationId }) {
  if (!isValidConsultationId(consultationId)) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'continue_from must be a bridge consultation id.',
    );
  }

  const consultationsRoot = path.resolve(rootDir, '.consultations');
  let realRoot;
  try {
    realRoot = await fs.realpath(consultationsRoot);
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw new BridgeError(
        FAILURE_CODES.CONTINUATION_RECEIPT_NOT_FOUND,
        'The continuation receipt directory was not found.',
        error,
      );
    }
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt directory could not be verified.',
      error,
    );
  }
  if (path.resolve(realRoot) !== consultationsRoot) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt directory resolves outside the bridge root.',
    );
  }

  const expectedReceiptPath = path.join(consultationsRoot, consultationId, 'receipt.json');
  let realReceiptPath;
  try {
    realReceiptPath = await fs.realpath(expectedReceiptPath);
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw new BridgeError(
        FAILURE_CODES.CONTINUATION_RECEIPT_NOT_FOUND,
        'The continuation receipt was not found.',
        error,
      );
    }
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt path could not be verified.',
      error,
    );
  }

  const expectedRelative = path.join(consultationId, 'receipt.json');
  const actualRelative = path.relative(realRoot, realReceiptPath);
  if (!isPathInside(realRoot, realReceiptPath) || actualRelative !== expectedRelative) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt path is outside its consultation directory.',
    );
  }

  let receipt;
  try {
    receipt = JSON.parse(await fs.readFile(realReceiptPath, 'utf8'));
  } catch (error) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt is not valid JSON.',
      error,
    );
  }
  if (!validateContinuationReceipt(receipt, consultationId)) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'The continuation receipt is incomplete, failed, or not bridge-created.',
    );
  }
  return { receipt, receiptPath: realReceiptPath };
}

async function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        server.close(() => reject(new Error('Could not allocate a local Chromium CDP port.')));
        return;
      }
      server.close(() => resolve(address.port));
    });
  });
}

async function waitForCdp(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return;
    } catch {
      // The detached Chromium process may need a few seconds to expose CDP.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('Chromium CDP did not become ready.');
}

export function buildReceipt({
  consultationId,
  createdAt,
  profile,
  chatUrl = CHATGPT_URL,
  mode = DEFAULT_CONVERSATION_MODE,
  conversationId = null,
  parentConsultationId = null,
  conversationRootConsultationId = null,
  conversationValidated = false,
  status,
  responseCharCount = 0,
  failureCode,
  requestCount = MAX_CHATGPT_REQUESTS_PER_INVOCATION,
  attachments = [],
  contextPack = undefined,
  transport = undefined,
  diagnostics = undefined,
  responseForensic = undefined,
  browserCheckpoints = undefined,
  projectNavigationDiagnostics = undefined,
  project_navigation_diagnostics = undefined,
  projectUrl = undefined,
  project_url = undefined,
  projectScopeRequested = undefined,
  project_scope_requested = undefined,
  projectScopeVerified = undefined,
  project_scope_verified = undefined,
  projectScopeEvidence = undefined,
  project_scope_evidence = undefined,
  requestedProjectUrl = undefined,
  requested_project_url = undefined,
}) {
  const receipt = {
    consultation_id: consultationId,
    created_at: createdAt,
    mode,
    request_count: requestCount,
    profile,
    chat_url: chatUrl,
    conversation_id: conversationId,
    parent_consultation_id: parentConsultationId,
    conversation_root_consultation_id: conversationRootConsultationId,
    conversation_validated: conversationValidated,
    status,
    response_char_count: responseCharCount,
  };
  if (Array.isArray(attachments) && attachments.length > 0) receipt.attachments = attachments;
  if (contextPack !== undefined) receipt.context_pack = contextPackReceiptMetadata(contextPack);
  const normalizedTransport = safeTransport(transport);
  if (normalizedTransport) receipt.transport = normalizedTransport;

  let normalizedProjectUrl = null;
  try {
    normalizedProjectUrl = resolveProjectUrlAlias({ projectUrl, project_url });
  } catch {
    // Receipt construction is a sanitizer boundary. Invalid project input is
    // never copied into a receipt; consultOnce reports PROJECT_URL_INVALID.
  }
  // A homepage fallback intentionally has no verified Project scope. Preserve
  // an optional requested URL as bounded metadata under a distinct field.
  if (normalizedTransport === TRANSPORTS.HOMEPAGE_FALLBACK) {
    let normalizedRequestedProjectUrl = null;
    try {
      normalizedRequestedProjectUrl = resolveProjectUrlAlias({
        projectUrl: requestedProjectUrl,
        project_url: requested_project_url,
      });
    } catch {
      // Invalid optional metadata is discarded at the receipt boundary.
    }
    if (normalizedRequestedProjectUrl) receipt.requested_project_url = normalizedRequestedProjectUrl;
  } else if (normalizedProjectUrl) {
    receipt.project_url = normalizedProjectUrl;
    const requested = projectScopeRequested === undefined ? project_scope_requested : projectScopeRequested;
    const verified = projectScopeVerified === undefined ? project_scope_verified : projectScopeVerified;
    const evidence = projectScopeEvidence === undefined ? project_scope_evidence : projectScopeEvidence;
    receipt.project_scope_requested = requested === undefined
      ? true
      : requested === true;
    receipt.project_scope_verified = verified === true;
    const safeProjectEvidence = safeProjectScopeEvidence(evidence, normalizedProjectUrl);
    if (safeProjectEvidence) receipt.project_scope_evidence = safeProjectEvidence;
  }
  const boundTargetUrl = normalizedTransport === TRANSPORTS.HOMEPAGE_FALLBACK
    ? null
    : normalizedProjectUrl;
  receipt.chatgpt_target_mode = boundTargetUrl ? 'PROJECT' : 'DEFAULT';
  receipt.chatgpt_target_url_digest = boundTargetUrl
    ? crypto.createHash('sha256').update(boundTargetUrl, 'utf8').digest('hex')
    : null;
  receipt.chatgpt_target_origin = CHATGPT_ORIGIN;
  receipt.chatgpt_project_target_verified = boundTargetUrl && projectScopeVerified === true ? 'YES' : 'NO';
  receipt.fresh_project_chat_created = boundTargetUrl
    && mode === CONVERSATION_MODES.FRESH
    && status === 'complete'
    && conversationValidated === true
    ? 'YES'
    : 'NO';
  const safeDiagnostics = safeAttachmentDiagnostics(diagnostics);
  if (safeDiagnostics) receipt.attachment_diagnostics = safeDiagnostics;
  const safeResponseDiagnostics = safeResponseForensic(responseForensic);
  if (safeResponseDiagnostics) receipt.response_forensic = safeResponseDiagnostics;
  const safeCheckpoints = safeBrowserCheckpoints(browserCheckpoints);
  if (safeCheckpoints) receipt.browser_checkpoints = safeCheckpoints;
  const navigationDiagnostics = projectNavigationDiagnostics === undefined
    ? project_navigation_diagnostics
    : projectNavigationDiagnostics;
  const safeNavigationDiagnostics = safeProjectNavigationDiagnostics(navigationDiagnostics);
  if (safeNavigationDiagnostics) receipt.project_navigation_diagnostics = safeNavigationDiagnostics;
  if (failureCode) {
    receipt.failure_code = failureCode;
    receipt.failure_class = failureClassForCode(failureCode);
  }
  return receipt;
}

async function writePrivateFile(filePath, contents) {
  await fs.writeFile(filePath, contents, { encoding: 'utf8', mode: 0o600 });
}

export async function writeConsultationArtifacts({
  rootDir,
  consultationId,
  createdAt,
  prompt,
  profile,
  chatUrl = CHATGPT_URL,
  mode = DEFAULT_CONVERSATION_MODE,
  conversationId = null,
  parentConsultationId = null,
  conversationRootConsultationId = null,
  conversationValidated = false,
  status,
  responseText = '',
  failureCode,
  requestCount = 0,
  attachments = [],
  contextPack = undefined,
  transport = undefined,
  diagnostics = undefined,
  responseForensic = undefined,
  browserCheckpoints = undefined,
  projectNavigationDiagnostics = undefined,
  project_navigation_diagnostics = undefined,
  projectUrl = undefined,
  project_url = undefined,
  projectScopeRequested = undefined,
  project_scope_requested = undefined,
  projectScopeVerified = undefined,
  project_scope_verified = undefined,
  projectScopeEvidence = undefined,
  project_scope_evidence = undefined,
  requestedProjectUrl = undefined,
  requested_project_url = undefined,
}) {
  const consultationDir = path.join(rootDir, '.consultations', consultationId);
  await fs.mkdir(consultationDir, { recursive: true, mode: 0o700 });
  const requestPath = path.join(consultationDir, 'request.txt');
  const responsePath = path.join(consultationDir, 'response.txt');
  const receiptPath = path.join(consultationDir, 'receipt.json');
  // Prompt and complete assistant responses are transport material.  Keep
  // them in memory for the current invocation only; consultation directories
  // retain the bounded receipt needed for continuation and auditability.
  const receipt = buildReceipt({
    consultationId,
    createdAt,
    profile,
    chatUrl,
    mode,
    conversationId,
    parentConsultationId,
    conversationRootConsultationId,
    conversationValidated,
    status,
    responseCharCount: responseText.length,
    failureCode,
    requestCount,
    attachments,
    contextPack,
    transport,
    diagnostics,
    responseForensic,
    browserCheckpoints,
    projectNavigationDiagnostics,
    project_navigation_diagnostics,
    projectUrl,
    project_url,
    projectScopeRequested,
    project_scope_requested,
    projectScopeVerified,
    project_scope_verified,
    projectScopeEvidence,
    project_scope_evidence,
    requestedProjectUrl,
    requested_project_url,
  });
  await writePrivateFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  return { consultationDir, requestPath, responsePath, receiptPath, receipt };
}

export class ChatGPTBridge {
  constructor({
    profileDir = DEFAULT_PROFILE_DIR,
    mode = undefined,
    responseTimeoutMs = DEFAULT_RESPONSE_TIMEOUT_MS,
    navigationTimeoutMs = 60_000,
    stabilityMs = 1_500,
    pollMs = 500,
    attachmentUploadTimeoutMs = 30_000,
    log = () => {},
  } = {}) {
    this.profileDir = path.resolve(profileDir);
    this.mode = mode;
    this.responseTimeoutMs = normalizeResponseTimeoutMs(responseTimeoutMs);
    this.navigationTimeoutMs = navigationTimeoutMs;
    this.stabilityMs = stabilityMs;
    this.pollMs = pollMs;
    this.attachmentUploadTimeoutMs = attachmentUploadTimeoutMs;
    this.log = log;
    this.context = null;
    this.page = null;
    this.browser = null;
    this.browserProcess = null;
    this.requestCount = 0;
    this.projectNavigationDiagnostics = null;
  }

  async open() {
    this.log(`launching profile=${this.profileDir}`);
    await fs.mkdir(this.profileDir, { recursive: true, mode: 0o700 });
    try {
      const port = await findFreePort();
      const executable = chromium.executablePath();
      this.browserProcess = spawn(executable, [
        '--user-data-dir=' + this.profileDir,
        '--remote-debugging-address=127.0.0.1',
        `--remote-debugging-port=${port}`,
        '--no-first-run',
        '--no-default-browser-check',
      ], {
        detached: true,
        stdio: 'ignore',
        windowsHide: false,
      });
      this.browserProcess.unref();
      await waitForCdp(port, 30_000);
      this.browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
      this.context = this.browser.contexts()[0];
      if (!this.context) throw new Error('Chromium CDP did not expose a browser context.');
      this.page = this.context.pages()[0] || await this.context.newPage();
      this.page.setDefaultTimeout(10_000);
      this.page.setDefaultNavigationTimeout(this.navigationTimeoutMs);
      return this;
    } catch (error) {
      // A browser that never exposes its CDP endpoint is a bounded
      // pre-prompt transport failure.  Preserve the established generic
      // launch error for other configuration failures, but classify the
      // readiness timeout so consultOnce can perform its small, explicit
      // fresh-cycle recovery instead of consuming the semantic request.
      const message = String(error?.message || '');
      if (message.includes('Chromium CDP did not become ready')) {
        const failure = new BridgeError(
          FAILURE_CODES.BRIDGE_TIMEOUT,
          failureMessage(FAILURE_CODES.BRIDGE_TIMEOUT),
          error,
        );
        failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.TIMEOUT;
        failure.diagnostics = {
          failure_phase: 'BRIDGE_OPEN',
          failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.TIMEOUT,
        };
        throw failure;
      }
      if (/target closed|browser has been closed|page has been closed|context has been closed/i.test(message)) {
        const failure = new BridgeError(
          FAILURE_CODES.TARGET_CLOSED,
          failureMessage(FAILURE_CODES.TARGET_CLOSED),
          error,
        );
        failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED;
        failure.diagnostics = {
          failure_phase: 'BRIDGE_OPEN',
          failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED,
        };
        throw failure;
      }
      throw new BridgeError(FAILURE_CODES.UNEXPECTED_PAGE_STATE, 'Could not launch headed Chromium.', error);
    }
  }

  async close() {
    const browser = this.browser;
    const browserProcess = this.browserProcess;
    this.browser = null;
    this.browserProcess = null;
    this.context = null;
    this.page = null;
    try {
      await browser?.close();
    } catch {
      // The detached browser may already have exited after a failed navigation.
    }
    if (!browserProcess || processHasExited(browserProcess)) return true;

    // Give the owned process a bounded opportunity to exit after the CDP
    // browser closes.  Do not signal any process that this bridge did not
    // spawn; browserProcess is captured only from open().
    let exited = await waitForChildProcessExit(
      browserProcess,
      BROWSER_PROCESS_EXIT_TIMEOUT_MS,
    );
    if (!exited && !browserProcess.killed) {
      try {
        browserProcess.kill();
      } catch {
        // Child-process cleanup is best effort; no credentials are involved.
      }
    }
    if (!exited) {
      exited = await waitForChildProcessExit(
        browserProcess,
        BROWSER_PROCESS_KILL_WAIT_MS,
      );
    }
    return exited;
  }

  releaseForManualLogin() {
    // Keep the headed window available for the user; the CLI exits immediately after reporting LOGIN_REQUIRED.
    this.browser = null;
    this.browserProcess = null;
    this.context = null;
    this.page = null;
  }

  async navigate() {
    return this.navigateTo(CHATGPT_URL, FAILURE_CODES.CHATGPT_NAVIGATION_FAILED);
  }

  async navigateTo(url, failureCode = FAILURE_CODES.CHATGPT_NAVIGATION_FAILED) {
    this.#assertOpen();
    if (failureCode === FAILURE_CODES.PROJECT_NAVIGATION_FAILED) {
      return this.navigateToProject(url);
    }
    this.log(`opening ${url}`);
    try {
      await this.page.goto(url, {
        waitUntil: 'domcontentloaded',
        timeout: this.navigationTimeoutMs,
      });
      await this.page.waitForTimeout(750);
      return this.page.url();
    } catch (error) {
      throw new BridgeError(failureCode, failureMessage(failureCode), error);
    }
  }

  /**
   * Navigate to a canonical project landing route with one bounded,
   * navigation-only retry for transient page-load failures. The returned URL
   * remains the raw browser URL so the caller's strict scope verification can
   * reject query/hash/redirect mismatches; only the receipt diagnostics are
   * sanitized.
   */
  async navigateToProject(url) {
    this.#assertOpen();
    let requestedUrl;
    try {
      requestedUrl = normalizeProjectUrl(url);
    } catch (error) {
      this.projectNavigationDiagnostics = projectNavigationDiagnosticsForAttempt({
        requestedUrl: null,
        attemptCount: 0,
        retryCount: 0,
        startedAt: Date.now(),
        failureClass: PROJECT_NAVIGATION_FAILURE_CLASSES.INVALID_URL,
        error,
      });
      throw error;
    }

    const startedAt = Date.now();
    let attemptCount = 0;
    let retryCount = 0;
    // ChatGPT's Project route depends on the root application bootstrap
    // (session hydration and challenge state). Establish that state before
    // the first Project navigation when this is a real Playwright page. The
    // capability check also keeps this method deterministic for the bounded
    // page doubles used by the offline navigation tests.
    const canBootstrapRoot = typeof this.page?.locator === 'function';
    const bootstrapUrl = canBootstrapRoot ? CHATGPT_URL : null;
    if (canBootstrapRoot) {
      this.log('bootstrapping ChatGPT root before project navigation');
      let bootstrapResponse;
      try {
        bootstrapResponse = await this.page.goto(CHATGPT_URL, {
          waitUntil: 'domcontentloaded',
          timeout: this.navigationTimeoutMs,
        });
      } catch (error) {
        const classified = classifyProjectNavigationError(error);
        const failureClass = classified === PROJECT_NAVIGATION_FAILURE_CLASSES.UNKNOWN
          ? PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP
          : classified;
        const diagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount: 0,
          retryCount: 0,
          startedAt,
          landingUrl: safePageUrl(this.page),
          title: await safePageTitle(this.page),
          failureClass,
          error,
          bootstrapUsed: true,
          bootstrapUrl,
        });
        this.projectNavigationDiagnostics = diagnostics;
        const failure = new BridgeError(
          FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
          failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
          error,
        );
        failure.failureClass = failureClass;
        failure.diagnostics = diagnostics;
        failure.projectNavigationDiagnostics = diagnostics;
        throw failure;
      }
      const bootstrapStatus = safeNavigationStatus(bootstrapResponse);
      if (bootstrapStatus !== null && bootstrapStatus >= 400) {
        const diagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount: 0,
          retryCount: 0,
          startedAt,
          status: bootstrapStatus,
          landingUrl: safePageUrl(this.page),
          title: await safePageTitle(this.page),
          failureClass: bootstrapStatus === 403
            ? PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE
            : PROJECT_NAVIGATION_FAILURE_CLASSES.HTTP_ERROR,
          bootstrapUsed: true,
          bootstrapUrl,
        });
        this.projectNavigationDiagnostics = diagnostics;
        const failure = new BridgeError(
          FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
          failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
        );
        failure.diagnostics = diagnostics;
        failure.projectNavigationDiagnostics = diagnostics;
        throw failure;
      }
      const rootReady = await waitForComposerOrLogin(this.page, {
        timeoutMs: COMPOSER_READY_TIMEOUT_MS,
        pollMs: 250,
      });
      if (rootReady.loginRequired) {
        const failure = new BridgeError(
          FAILURE_CODES.LOGIN_REQUIRED,
          failureMessage(FAILURE_CODES.LOGIN_REQUIRED),
        );
        failure.projectNavigationDiagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount: 0,
          retryCount: 0,
          startedAt,
          landingUrl: safePageUrl(this.page),
          title: await safePageTitle(this.page),
          failureClass: PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE,
          bootstrapUsed: true,
          bootstrapUrl,
        });
        failure.diagnostics = failure.projectNavigationDiagnostics;
        throw failure;
      }
      if (!rootReady.composer) {
        const failure = new BridgeError(
          FAILURE_CODES.UNEXPECTED_PAGE_STATE,
          'ChatGPT root bootstrap did not produce a visible composer.',
        );
        failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP;
        failure.projectNavigationDiagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount: 0,
          retryCount: 0,
          startedAt,
          landingUrl: safePageUrl(this.page),
          title: await safePageTitle(this.page),
          failureClass: PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP,
          bootstrapUsed: true,
          bootstrapUrl,
        });
        failure.diagnostics = failure.projectNavigationDiagnostics;
        throw failure;
      }
    }
    while (attemptCount <= MAX_PROJECT_NAVIGATION_RETRIES) {
      attemptCount += 1;
      let response = null;
      let status = null;
      let rawLandingUrl = null;
      let title = null;
      try {
        this.log(`opening project ${requestedUrl} attempt=${attemptCount}`);
        // Use the hydrated app's own Project link when available.  A direct
        // deep-link GET can leave the Project shell in its retry-only state;
        // clicking the already-mounted link preserves the root bootstrap and
        // lets the SPA perform its normal transition. Fall back to goto only
        // when the link is not rendered in the current session.
        const projectPath = new URL(requestedUrl).pathname;
        const projectLink = canBootstrapRoot
          ? this.page.locator(`a[href="${projectPath}"]`).first()
          : null;
        if (projectLink && await projectLink.count() > 0 && await projectLink.isVisible()) {
          await projectLink.click();
          await this.page.waitForURL((value) => value.pathname === projectPath, {
            timeout: this.navigationTimeoutMs,
          });
          response = null;
        } else {
          response = await this.page.goto(requestedUrl, {
            waitUntil: 'domcontentloaded',
            timeout: this.navigationTimeoutMs,
          });
        }
        status = safeNavigationStatus(response);
        rawLandingUrl = safePageUrl(this.page);
        title = await safePageTitle(this.page);

        // HTTP failures, including 403 challenge pages, are terminal. A
        // second GET would not make a challenge safer or more deterministic.
        if (status !== null && status >= 400) {
          const failureClass = status === 403
            ? PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE
            : PROJECT_NAVIGATION_FAILURE_CLASSES.HTTP_ERROR;
          const diagnostics = projectNavigationDiagnosticsForAttempt({
            requestedUrl,
            attemptCount,
            retryCount,
            startedAt,
            status,
            title,
            landingUrl: rawLandingUrl,
            failureClass,
            bootstrapUsed: canBootstrapRoot,
            bootstrapUrl,
          });
          this.projectNavigationDiagnostics = diagnostics;
          const failure = new BridgeError(
            FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
            failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
          );
          failure.diagnostics = diagnostics;
          failure.projectNavigationDiagnostics = diagnostics;
          throw failure;
        }

        await this.#settleProjectNavigation();
        const projectReady = canBootstrapRoot
          ? await waitForComposerOrLogin(this.page, {
            timeoutMs: COMPOSER_READY_TIMEOUT_MS,
            pollMs: 250,
          })
          : { loginRequired: false, composer: true };
        if (projectReady.loginRequired) {
          const failure = new BridgeError(
            FAILURE_CODES.LOGIN_REQUIRED,
            failureMessage(FAILURE_CODES.LOGIN_REQUIRED),
          );
          failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.CHALLENGE;
          throw failure;
        }
        if (!projectReady.composer) {
          const failure = new BridgeError(
            FAILURE_CODES.UNEXPECTED_PAGE_STATE,
            'Project bootstrap did not produce a visible composer.',
          );
          failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP;
          throw failure;
        }
        rawLandingUrl = safePageUrl(this.page) || rawLandingUrl;
        title = (await safePageTitle(this.page)) || title;
        const diagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount,
          retryCount,
          startedAt,
          status,
          title,
          landingUrl: rawLandingUrl,
          failureClass: PROJECT_NAVIGATION_FAILURE_CLASSES.NONE,
          bootstrapUsed: canBootstrapRoot,
          bootstrapUrl,
        });
        this.projectNavigationDiagnostics = diagnostics;
        if (!rawLandingUrl) {
          throw new Error('Project navigation did not expose a landing URL.');
        }
        return rawLandingUrl;
      } catch (error) {
        // Preserve the explicit HTTP failure branch above; it must not be
        // retried or reclassified as a transient browser exception.
        if (error instanceof BridgeError && error.code === FAILURE_CODES.PROJECT_NAVIGATION_FAILED) {
          throw error;
        }
        const failureClass = classifyProjectNavigationError(error);
        rawLandingUrl = rawLandingUrl || safePageUrl(this.page);
        title = title || await safePageTitle(this.page);
        const diagnostics = projectNavigationDiagnosticsForAttempt({
          requestedUrl,
          attemptCount,
          retryCount,
          startedAt,
          status,
          title,
          landingUrl: rawLandingUrl,
          failureClass,
          error,
          bootstrapUsed: canBootstrapRoot,
          bootstrapUrl,
        });
        this.projectNavigationDiagnostics = diagnostics;
        const canRetry = attemptCount <= MAX_PROJECT_NAVIGATION_RETRIES
          && (isRetryableProjectNavigationError(error)
            || (error instanceof BridgeError
              && error.code === FAILURE_CODES.PROMPT_INPUT_NOT_FOUND));
        if (canRetry) {
          retryCount += 1;
          this.log(`project navigation transient failure class=${failureClass}; retrying once`);
          try {
            await this.#settleProjectNavigation();
            if (failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED) {
              // A closed page/context is not a transient state that can be
              // retried on the same Playwright object.  Recycle only this
              // bridge's owned browser/page, then perform the one permitted
              // navigation-only retry.  No prompt, upload, or conversation
              // operation is repeated here.
              await this.#recoverFromTargetClosed();
            } else if (canBootstrapRoot) {
              await this.page.goto(CHATGPT_URL, {
                waitUntil: 'domcontentloaded',
                timeout: this.navigationTimeoutMs,
              });
              const recoveredRoot = await waitForComposerOrLogin(this.page, {
                timeoutMs: COMPOSER_READY_TIMEOUT_MS,
                pollMs: 250,
              });
              if (recoveredRoot.loginRequired || !recoveredRoot.composer) {
                throw new Error('ChatGPT root bootstrap did not recover before Project retry.');
              }
            }
          } catch (settleError) {
            // Keep the original target lifecycle failure visible when
            // recovery itself cannot complete; classifying only the cleanup
            // error as UNKNOWN would hide the fact that no second page.goto
            // was attempted.
            const settledFailureClass = failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED
              ? PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED
              : classifyProjectNavigationError(settleError);
            const settledDiagnostics = projectNavigationDiagnosticsForAttempt({
              requestedUrl,
              attemptCount,
              retryCount,
              startedAt,
              status: null,
              title: await safePageTitle(this.page),
              landingUrl: safePageUrl(this.page),
              failureClass: settledFailureClass,
              error: settleError,
              bootstrapUsed: canBootstrapRoot,
              bootstrapUrl,
            });
            this.projectNavigationDiagnostics = settledDiagnostics;
            const failure = new BridgeError(
              FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
              failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
              settleError,
            );
            failure.diagnostics = settledDiagnostics;
            failure.projectNavigationDiagnostics = settledDiagnostics;
            throw failure;
          }
          continue;
        }
        const failure = new BridgeError(
          FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
          failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
          error,
        );
        failure.diagnostics = diagnostics;
        failure.projectNavigationDiagnostics = diagnostics;
        throw failure;
      }
    }

    // The loop always returns or throws; keep a defensive fail-closed branch
    // in case the retry bound is changed incorrectly in a future edit.
    const failure = new BridgeError(
      FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
      failureMessage(FAILURE_CODES.PROJECT_NAVIGATION_FAILED),
    );
    failure.projectNavigationDiagnostics = projectNavigationDiagnosticsForAttempt({
      requestedUrl,
      attemptCount,
      retryCount,
      startedAt,
      failureClass: PROJECT_NAVIGATION_FAILURE_CLASSES.UNKNOWN,
      bootstrapUsed: canBootstrapRoot,
      bootstrapUrl,
    });
    failure.diagnostics = failure.projectNavigationDiagnostics;
    throw failure;
  }

  async #settleProjectNavigation() {
    if (typeof this.page?.waitForTimeout !== 'function') return;
    await this.page.waitForTimeout(PROJECT_NAVIGATION_RETRY_SETTLE_MS);
  }

  async #recoverFromTargetClosed() {
    this.log('project navigation target closed; recycling owned browser once');
    const closed = await this.close();
    if (closed === false) {
      throw new Error('Owned Chromium process did not exit before target recovery.');
    }
    await this.open();
    this.#assertOpen();
  }

  async createFreshConversation({ projectUrl, transport } = {}) {
    this.#assertOpen();
    let preserveProjectScope = false;
    if (projectUrl !== undefined) {
      try {
        preserveProjectScope = normalizeProjectUrl(projectUrl) === normalizeProjectUrl(this.page.url());
      } catch {
        // The project URL was validated before this method is called.  If the
        // browser is no longer on that exact landing route, fail closed below
        // instead of allowing the global New chat link to escape the Project.
      }
    }
    const result = await createFreshConversation(this.page, { preserveProjectScope });
    // The current ChatGPT homepage keeps a fresh composer on `/` and only
    // materializes `/c/<uuid>` when the first prompt is submitted.  Treat that
    // verified empty homepage composer as a deferred fresh conversation for
    // every homepage transport, not only the legacy explicit fallback mode.
    // Project scope remains fail-closed because a Project landing must never
    // escape to the global homepage.
    if (!preserveProjectScope && !result.deferred) {
      const home = isChatGptHomepageUrl(this.page.url());
      const composer = home ? await findComposer(this.page) : null;
      const history = home ? await readAssistantSnapshot(this.page) : [];
      const users = home ? await this.page.locator('[data-message-author-role="user"]').count() : -1;
      if (home && composer && history.length === 0 && users === 0) {
        return { ...result, deferred: true, conversationId: null };
      }
    }
    if (result.deferred === true) {
      return { ...result, deferred: true, conversationId: null };
    }
    const conversationId = extractConversationIdFromUrl(result.afterUrl);
    if (!result.clicked || !conversationId || (
      result.conversationId !== undefined && result.conversationId !== conversationId
    )) {
      throw new BridgeError(
        FAILURE_CODES.FRESH_CHAT_CREATION_FAILED,
        failureMessage(FAILURE_CODES.FRESH_CHAT_CREATION_FAILED),
      );
    }
    return { ...result, conversationId };
  }

  currentUrl() {
    this.#assertOpen();
    return this.page.url();
  }

  async waitForConversationRoute(expectedConversationId, {
    timeoutMs = CONVERSATION_ROUTE_SETTLE_TIMEOUT_MS,
    pollMs = CONVERSATION_ROUTE_SETTLE_POLL_MS,
  } = {}) {
    this.#assertOpen();
    const expected = typeof expectedConversationId === 'string' ? expectedConversationId : null;
    const timeout = Number.isFinite(Number(timeoutMs))
      ? Math.max(0, Math.min(CONVERSATION_ROUTE_SETTLE_TIMEOUT_MS, Number(timeoutMs)))
      : CONVERSATION_ROUTE_SETTLE_TIMEOUT_MS;
    const interval = Number.isFinite(Number(pollMs))
      ? Math.max(0, Math.min(1_000, Number(pollMs)))
      : CONVERSATION_ROUTE_SETTLE_POLL_MS;
    const deadline = Date.now() + timeout;
    let currentUrl = this.currentUrl();
    while (true) {
      currentUrl = this.currentUrl();
      const currentConversationId = extractConversationIdFromUrl(currentUrl);
      if (currentConversationId && (!expected || currentConversationId === expected)) return currentUrl;
      if (Date.now() >= deadline) return currentUrl;
      await this.page.waitForTimeout(Math.min(interval, Math.max(0, deadline - Date.now())));
    }
  }

  /**
   * Reload only a verified conversation route for SPA hydration recovery.
   * Project composers can render a response before the route transition is
   * visible to Playwright, and ChatGPT may redirect a reload to `/`.  Save the
   * route first, wait briefly for a pending route transition, then restore the
   * saved route if the reload lands anywhere else.  This method never fills,
   * clicks, or submits a composer.
   *
   * @returns {Promise<boolean>} false when no conversation route was observed
   * before the bounded wait; callers keep observing the existing page.
   */
  async reloadConversationForHydration() {
    this.#assertOpen();
    let savedRoute = sanitizedConversationRoute(this.currentUrl());
    if (!savedRoute) {
      const settledUrl = await this.waitForConversationRoute(undefined, {
        timeoutMs: CONVERSATION_ROUTE_SETTLE_TIMEOUT_MS,
        // Route polling is deliberately slower than the 50ms transition
        // settle default: this is recovery telemetry, not a hot UI loop.
        pollMs: Math.max(250, Number(this.pollMs) || 0),
      });
      savedRoute = sanitizedConversationRoute(settledUrl);
    }
    if (!savedRoute) {
      this.log('conversation hydration route not ready; skipping reload');
      return false;
    }

    const navigationTimeout = Math.max(
      1,
      Math.min(
        CONVERSATION_HYDRATION_NAVIGATION_TIMEOUT_MS,
        Number.isFinite(Number(this.navigationTimeoutMs)) ? Number(this.navigationTimeoutMs) : CONVERSATION_HYDRATION_NAVIGATION_TIMEOUT_MS,
      ),
    );
    this.log('reloading saved conversation route for hydration');
    await this.page.reload({ waitUntil: 'domcontentloaded', timeout: navigationTimeout });
    await this.#settleProjectNavigation();

    if (sanitizedConversationRoute(this.currentUrl()) !== savedRoute) {
      this.log('restoring saved conversation route after hydration reload');
      await this.page.goto(savedRoute, {
        waitUntil: 'domcontentloaded',
        timeout: navigationTimeout,
      });
      await this.#settleProjectNavigation();
    }

    if (sanitizedConversationRoute(this.currentUrl()) !== savedRoute) {
      throw new Error('Conversation hydration reload did not preserve the verified route.');
    }
    return true;
  }

  async ensureLoggedIn() {
    this.#assertOpen();
    this.log('checking login state');
    const result = await waitForComposerOrLogin(this.page, {
      timeoutMs: Math.min(this.navigationTimeoutMs, COMPOSER_READY_TIMEOUT_MS),
      pollMs: 250,
    });
    if (result.composer) return result.composer;
    if (result.loginRequired) {
      throw new BridgeError(
        FAILURE_CODES.LOGIN_REQUIRED,
        'Login is required. Use the opened headed Chromium window and log in manually.',
      );
    }
    throw new BridgeError(
      FAILURE_CODES.PROMPT_INPUT_NOT_FOUND,
      'Could not find a visible ChatGPT message composer.',
    );
  }

  async waitForAssistantBaseline({ requireNonEmpty = false } = {}) {
    this.#assertOpen();
    try {
      return await waitForStableAssistantSnapshot(
        () => readAssistantSnapshot(this.page),
        {
          requireNonEmpty,
          timeoutMs: Math.min(this.responseTimeoutMs, 15_000),
          pollMs: this.pollMs,
          stabilityMs: this.stabilityMs,
          sleep: (ms) => this.page.waitForTimeout(ms),
        },
      );
    } catch (error) {
      throw new BridgeError(
        FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
        'Existing assistant messages did not reach a stable baseline.',
        error,
      );
    }
  }

  async uploadPreparedAttachments(files, { mode = this.mode, requestCount = this.requestCount } = {}) {
    this.#assertOpen();
    if (!Array.isArray(files) || files.length === 0) return null;
    try {
      return await uploadAttachments(this.page, files.map((file) => file.realPath), {
        expectedBasenames: files.map((file) => file.basename),
        timeoutMs: this.attachmentUploadTimeoutMs,
        pollMs: this.pollMs,
        diagnosticContext: { mode, requestCount },
      });
    } catch (error) {
      try {
        await clearAttachmentFiles(this.page);
      } catch (cleanupError) {
        this.log(`attachment cleanup failed: ${cleanupError?.message || 'unknown error'}`);
      }
      const code = error?.message?.startsWith('Timed out')
        ? FAILURE_CODES.ATTACHMENT_NOT_READY
        : FAILURE_CODES.ATTACHMENT_UPLOAD_FAILED;
      const failure = new BridgeError(code, failureMessage(code), error);
      const diagnostics = safeAttachmentDiagnostics({
        ...(error?.diagnostics || {}),
        mode,
        request_count: this.requestCount,
      });
      if (diagnostics) {
        failure.diagnostics = diagnostics;
        this.log(`attachment diagnostics ${JSON.stringify(diagnostics)}`);
      }
      throw failure;
    }
  }

  /**
   * Re-check the hard project invariant immediately before submission. This
   * is deliberately independent from the earlier navigation checkpoint: a
   * SPA redirect, stale page, or composer escape must fail closed at the last
   * possible point before the one allowed request.
   */
  async verifyProjectScopeBeforeSend(projectUrl, { allowConversationRoute = false } = {}) {
    this.#assertOpen();
    let expectedUrl;
    try {
      expectedUrl = normalizeProjectUrl(projectUrl);
    } catch (error) {
      throw asBridgeError(error, FAILURE_CODES.PROJECT_URL_INVALID);
    }
    const actualRouteUrl = safePageUrl(this.page);
    const actualUrl = safeProjectScopeUrl(actualRouteUrl);
    const isBoundConversation = allowConversationRoute
      && isValidProjectConversationUrl(actualRouteUrl, expectedUrl);
    if ((!actualUrl || actualUrl !== expectedUrl) && !isBoundConversation) {
      throw new BridgeError(
        FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
        failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
      );
    }
    const composer = typeof this.page?.locator === 'function'
      ? await findComposer(this.page)
      : true;
    if (!composer) {
      const failure = new BridgeError(
        FAILURE_CODES.PROMPT_INPUT_NOT_FOUND,
        failureMessage(FAILURE_CODES.PROMPT_INPUT_NOT_FOUND),
      );
      failure.failureClass = PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP;
      throw failure;
    }
    return {
      expectedProjectUrl: expectedUrl,
      actualProjectUrl: expectedUrl,
      actualRouteUrl,
      composerReadyInProject: true,
    };
  }

  async sendOnePrompt(prompt, { baselineSnapshot } = {}) {
    this.#assertOpen();
    if (typeof prompt !== 'string' || !prompt.trim()) {
      throw new BridgeError(FAILURE_CODES.PROMPT_SEND_FAILED, 'Prompt must be a non-empty string.');
    }

    let baseline = baselineSnapshot;
    if (baseline === undefined) {
      try {
        baseline = await readAssistantSnapshot(this.page);
        validateStableAssistantSnapshot(baseline);
      } catch (error) {
        throw new BridgeError(
          FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
          'Existing assistant messages do not expose unique stable identifiers.',
          error,
        );
      }
    } else {
      try {
        validateStableAssistantSnapshot(baseline);
      } catch (error) {
        throw new BridgeError(
          FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
          'The supplied assistant baseline is invalid.',
          error,
        );
      }
    }

    const composer = await findComposer(this.page);
    if (!composer) {
      throw new BridgeError(FAILURE_CODES.PROMPT_INPUT_NOT_FOUND, 'Could not find a visible ChatGPT message composer.');
    }
    try {
      await composer.fill(prompt);
    } catch (error) {
      throw new BridgeError(FAILURE_CODES.PROMPT_SEND_FAILED, 'Could not fill the ChatGPT message composer.', error);
    }

    let send;
    try {
      send = await findSendButton(this.page);
      if (send) {
        const state = await send.evaluate((element) => ({
          disabled: Boolean(element.disabled),
          ariaDisabled: element.getAttribute('aria-disabled') === 'true',
        }));
        if (state.disabled || state.ariaDisabled || !await send.isEnabled()) {
          throw new Error('The ChatGPT send control is not available.');
        }
      } else {
        // Some older UI variants expose no send button and accept Enter.
        // The composer itself was already found and filled above.
        if (!await composer.isEnabled()) throw new Error('The ChatGPT composer is not available.');
      }
    } catch (error) {
      throw new BridgeError(FAILURE_CODES.PROMPT_SEND_FAILED, 'Could not send the one-shot prompt.', error);
    }

    assertRequestBudget(this.requestCount + 1);
    const sendStartedAt = Date.now();
    this.requestCount += 1;
    this.log(`sending request=${this.requestCount}/${MAX_CHATGPT_REQUESTS_PER_INVOCATION}`);
    try {
      if (send) {
        await send.click();
      } else {
        await composer.press('Enter');
      }
    } catch (error) {
      throw new BridgeError(FAILURE_CODES.PROMPT_SEND_FAILED, 'Could not send the one-shot prompt.', error);
    }
    const sendCompletedAt = Date.now();

    this.log('waiting for new assistant response');
    try {
      return await waitForNewAssistantResponse(this.page, baseline.map((item) => item.id), {
        baselineCount: baseline.length,
        baselineSnapshot: baseline,
        startedAt: sendStartedAt,
        sendStartedAt,
        sendCompletedAt,
        timeoutMs: this.responseTimeoutMs,
        pollMs: this.pollMs,
        stabilityMs: this.stabilityMs,
        refresh: typeof this.page.reload === 'function'
          ? () => this.reloadConversationForHydration()
          : undefined,
      });
    } catch (error) {
      const code = error?.message?.startsWith('Timed out')
        ? FAILURE_CODES.RESPONSE_TIMEOUT
        : FAILURE_CODES.RESPONSE_EXTRACTION_FAILED;
      const failure = new BridgeError(code, failureMessage(code), error);
      if (error?.responseForensic) failure.responseForensic = error.responseForensic;
      throw failure;
    }
  }

  #assertOpen() {
    if (!this.page || !this.context) {
      throw new BridgeError(FAILURE_CODES.UNEXPECTED_PAGE_STATE, 'Bridge is not open.');
    }
  }
}

function assistantSlotKey(item) {
  if (item && typeof item.slotId === 'string' && item.slotId) return `slot:${item.slotId}`;
  if (item && typeof item.id === 'string' && item.id) return `id:${item.id}`;
  return null;
}

export function assertAssistantBaselineUnchanged(before, after) {
  validateStableAssistantSnapshot(before);
  validateStableAssistantSnapshot(after);
  const beforeSlots = new Set(before.map(assistantSlotKey).filter(Boolean));
  const afterSlots = new Set(after.map(assistantSlotKey).filter(Boolean));
  const sameSlotSet = beforeSlots.size === afterSlots.size
    && [...beforeSlots].every((key) => afterSlots.has(key));
  if (!sameSlotSet) {
    throw new BridgeError(
      FAILURE_CODES.RESPONSE_EXTRACTION_FAILED,
      'Attachment upload changed the assistant baseline unexpectedly.',
    );
  }
}

async function consultOnceSingle(
  prompt,
  {
    rootDir = process.cwd(),
    profileDir = DEFAULT_PROFILE_DIR,
    responseTimeoutMs = undefined,
    navigationTimeoutMs = 60_000,
    log = () => {},
    mode = DEFAULT_CONVERSATION_MODE,
    continueFrom,
    projectUrl,
    project_url,
    attachments,
    contextPack,
    transport,
    allowedAttachmentRoots,
    attachmentBaseDir,
    attachmentUploadTimeoutMs = 30_000,
    bridgeFactory = (options) => new ChatGPTBridge(options),
  } = {},
) {
  if (typeof prompt !== 'string' || !prompt.trim()) {
    throw new BridgeError(FAILURE_CODES.PROMPT_SEND_FAILED, 'Prompt must be a non-empty string.');
  }
  const effectiveResponseTimeoutMs = resolveResponseTimeoutMs(responseTimeoutMs);
  if (!Object.values(CONVERSATION_MODES).includes(mode)) {
    throw new BridgeError(FAILURE_CODES.UNEXPECTED_PAGE_STATE, 'Conversation mode must be fresh or continue.');
  }
  if (mode === CONVERSATION_MODES.FRESH && continueFrom !== undefined) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'fresh mode must not include continue_from.',
    );
  }
  if (mode === CONVERSATION_MODES.CONTINUE && (typeof continueFrom !== 'string' || !continueFrom.trim())) {
    throw new BridgeError(
      FAILURE_CODES.CONTINUATION_RECEIPT_INVALID,
      'continue mode requires continue_from.',
    );
  }
  let requestedProjectUrl;
  let projectUrlValidationError;
  try {
    requestedProjectUrl = resolveProjectUrlAlias({ projectUrl, project_url });
  } catch (error) {
    projectUrlValidationError = asBridgeError(error, FAILURE_CODES.PROJECT_URL_INVALID);
  }
  let transportValidationError;
  try {
    transport = normalizeTransport(transport);
  } catch (error) {
    transportValidationError = asBridgeError(error, FAILURE_CODES.UNEXPECTED_PAGE_STATE);
  }
  if (typeof bridgeFactory !== 'function') {
    throw new TypeError('bridgeFactory must be a function');
  }
  if (attachments !== undefined && !Array.isArray(attachments)) {
    throw new BridgeError(FAILURE_CODES.ATTACHMENT_INVALID, 'attachments must be an array of local paths.');
  }
  let safeContextPack = undefined;
  let contextPackValidationError = undefined;
  if (contextPack !== undefined) {
    try {
      // Validate the receipt-safe portion before the started receipt is
      // written. Full manifest/file integrity and secret checks happen in the
      // guarded section below, before a browser is opened or a prompt sent.
      contextPackReceiptMetadata(contextPack);
      safeContextPack = contextPack;
    } catch (error) {
      contextPackValidationError = asBridgeError(error, FAILURE_CODES.CONTEXT_PACK_INVALID);
    }
  }
  const effectiveAttachments = attachments === undefined && contextPack?.attachmentPaths
    ? contextPack.attachmentPaths
    : attachments;
  if (contextPack !== undefined && attachments !== undefined && Array.isArray(contextPack.attachmentPaths)) {
    const expected = contextPack.attachmentPaths.map((value) => path.resolve(String(value)));
    const supplied = attachments.map((value) => path.resolve(String(value)));
    if (expected.length !== supplied.length || expected.some((value, index) => value !== supplied[index])) {
      contextPackValidationError = new BridgeError(
        FAILURE_CODES.CONTEXT_PACK_INVALID,
        'Explicit attachments must match the selected context pack attachments.',
      );
    }
  }
  const resolvedRoot = path.resolve(rootDir);
  const resolvedProfile = path.resolve(profileDir);
  const consultationId = createConsultationId();
  const createdAt = new Date().toISOString();
  const profile = displayProfile(resolvedProfile, resolvedRoot);
  const initialParentConsultationId = mode === CONVERSATION_MODES.CONTINUE ? continueFrom : null;
  let conversationRootConsultationId = mode === CONVERSATION_MODES.FRESH ? consultationId : null;
  let parentConsultationId = initialParentConsultationId;
  let conversationId = null;
  let conversationValidated = false;
  let verifiedChatUrl = CHATGPT_URL;
  let projectScopeRequested = requestedProjectUrl !== undefined;
  let projectScopeVerified = false;
  let projectScopeEvidence = requestedProjectUrl
    ? projectScopeEvidenceForLanding(requestedProjectUrl, null)
    : undefined;
  let projectNavigationDiagnostics;
  let attachmentMetadata = Array.isArray(effectiveAttachments)
    ? effectiveAttachments.map((item) => attachmentPlaceholder(item))
    : [];
  let preparedAttachments = { files: [], metadata: attachmentMetadata };
  const browserCheckpoints = [];
  const markCheckpoint = (checkpoint, status, failureClass = undefined) => {
    const entry = { checkpoint, status };
    if (failureClass) entry.failure_class = failureClass;
    browserCheckpoints.push(entry);
  };
  await writeConsultationArtifacts({
    rootDir: resolvedRoot,
    consultationId,
    createdAt,
    prompt,
    profile,
    mode,
    conversationId,
    parentConsultationId,
    conversationRootConsultationId,
    conversationValidated,
    status: 'started',
    requestCount: 0,
    attachments: attachmentMetadata,
    contextPack: safeContextPack,
    transport,
    browserCheckpoints,
    projectNavigationDiagnostics,
    projectUrl: requestedProjectUrl,
    projectScopeRequested,
    projectScopeVerified,
    projectScopeEvidence,
  });
  let bridge = null;
  let keepBrowserOpen = false;
  let expectedConversationId = null;
  try {
    if (projectUrlValidationError) throw projectUrlValidationError;
    if (transportValidationError) throw transportValidationError;
    if (requestedProjectUrl !== undefined && transport === TRANSPORTS.HOMEPAGE_FALLBACK) {
      throw new BridgeError(
        FAILURE_CODES.PROJECT_SCOPE_REQUIRED,
        failureMessage(FAILURE_CODES.PROJECT_SCOPE_REQUIRED),
      );
    }
    if (contextPackValidationError) throw contextPackValidationError;
    if (contextPack !== undefined) {
      try {
        contextPack = await validateContextPackForConsult(contextPack, {
          expectedConversationMode: mode,
        });
        safeContextPack = contextPack;
      } catch (error) {
        throw asBridgeError(error, FAILURE_CODES.CONTEXT_PACK_INVALID);
      }
    }
    if (effectiveAttachments && effectiveAttachments.length > 0) {
      const roots = await resolveAllowedAttachmentRoots({ allowedAttachmentRoots });
      preparedAttachments = await prepareAttachments(effectiveAttachments, {
        allowedAttachmentRoots: roots,
        baseDir: attachmentBaseDir || resolvedRoot,
        profileDir: resolvedProfile,
        maxBytes: MAX_ATTACHMENT_BYTES,
      });
      attachmentMetadata = preparedAttachments.metadata;
    }
    bridge = bridgeFactory({
      profileDir: resolvedProfile,
      mode,
      responseTimeoutMs: effectiveResponseTimeoutMs,
      navigationTimeoutMs,
      attachmentUploadTimeoutMs,
      log,
    });
    if (mode === CONVERSATION_MODES.CONTINUE) {
      const continuation = await readContinuationReceipt({
        rootDir: resolvedRoot,
        consultationId: continueFrom,
      });
      const inherited = deriveContinuationLineage(continuation.receipt);
      const parentProjectUrl = continuation.receipt.project_url;
      const parentProjectScopeEvidence = continuation.receipt.project_scope_evidence;
      if (requestedProjectUrl !== undefined) {
        if (!parentProjectUrl || requestedProjectUrl !== parentProjectUrl) {
          projectScopeEvidence = projectScopeEvidenceForBinding(requestedProjectUrl, false);
          throw new BridgeError(
            FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
            failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
          );
        }
      } else if (parentProjectUrl) {
        // Inherit the parent binding when the caller omits the optional
        // project URL. This keeps continuation convenient while ensuring the
        // browser is still scoped to the receipt's project; a caller-provided
        // different URL was rejected above.
        requestedProjectUrl = parentProjectUrl;
        projectScopeRequested = true;
      }
      parentConsultationId = inherited.parentConsultationId;
      conversationRootConsultationId = inherited.conversationRootConsultationId;
      expectedConversationId = inherited.conversationId;

      // A verified Project continuation already has a concrete, scoped
      // conversation route in its parent receipt.  Re-loading the Project
      // landing page here is redundant and can trigger a fresh HTTP challenge
      // in the restored headed profile before the conversation is reached.
      // Validate the parent route before opening Chromium so a global route,
      // wrong slug, or unverified scope can never be used as a fallback.
      if (
        requestedProjectUrl !== undefined
        && !isValidProjectConversationUrl(
          continuation.receipt.chat_url,
          requestedProjectUrl,
          expectedConversationId,
        )
      ) {
        projectScopeEvidence = projectScopeEvidenceForBinding(requestedProjectUrl, false);
        throw new BridgeError(
          FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
          failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
        );
      }

      await bridge.open();
      markCheckpoint('B0', BROWSER_CHECKPOINT_STATUSES.PASS);
      const landedUrl = await bridge.navigateTo(
        continuation.receipt.chat_url,
        FAILURE_CODES.CONTINUATION_CHAT_NOT_FOUND,
      );
      markCheckpoint('B1', BROWSER_CHECKPOINT_STATUSES.PASS);
      const landedConversationId = extractConversationIdFromUrl(landedUrl);
      if (!landedConversationId) {
        throw new BridgeError(
          FAILURE_CODES.CONTINUATION_CHAT_NOT_FOUND,
          failureMessage(FAILURE_CODES.CONTINUATION_CHAT_NOT_FOUND),
        );
      }
      if (landedConversationId !== expectedConversationId) {
        throw new BridgeError(
          FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH,
          failureMessage(FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH),
        );
      }
      if (
        requestedProjectUrl !== undefined
        && !isValidProjectConversationUrl(landedUrl, requestedProjectUrl, expectedConversationId)
      ) {
        projectScopeEvidence = projectScopeEvidenceForBinding(requestedProjectUrl, false);
        throw new BridgeError(
          FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
          failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
        );
      }
      if (requestedProjectUrl !== undefined) {
        projectScopeEvidence = projectScopeEvidenceForContinuation(
          requestedProjectUrl,
          parentProjectScopeEvidence,
        );
        if (!projectScopeEvidence) {
          projectScopeEvidence = projectScopeEvidenceForBinding(requestedProjectUrl, false);
          throw new BridgeError(
            FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
            failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
          );
        }
        projectScopeVerified = true;
      }
      await bridge.ensureLoggedIn();
      markCheckpoint('B2', BROWSER_CHECKPOINT_STATUSES.PASS);
      markCheckpoint('B4', BROWSER_CHECKPOINT_STATUSES.PASS);
    } else {
      await bridge.open();
      markCheckpoint('B0', BROWSER_CHECKPOINT_STATUSES.PASS);
      let beforeUrl;
      if (requestedProjectUrl !== undefined) {
        let landedProjectUrl;
        try {
          landedProjectUrl = await bridge.navigateTo(
            requestedProjectUrl,
            FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
          );
          projectNavigationDiagnostics = safeProjectNavigationDiagnostics(
            bridge.projectNavigationDiagnostics,
          ) || projectNavigationDiagnostics;
        } catch (error) {
          projectNavigationDiagnostics = safeProjectNavigationDiagnostics(
            error?.projectNavigationDiagnostics || bridge.projectNavigationDiagnostics,
          ) || projectNavigationDiagnostics;
          throw asBridgeError(error, FAILURE_CODES.PROJECT_NAVIGATION_FAILED);
        }
        projectScopeEvidence = projectScopeEvidenceForLanding(requestedProjectUrl, landedProjectUrl);
        if (!projectScopeEvidence.initial_navigation.matched) {
          projectNavigationDiagnostics = safeProjectNavigationDiagnostics({
            ...projectNavigationDiagnostics,
            failure_class: PROJECT_NAVIGATION_FAILURE_CLASSES.URL_MISMATCH,
          }) || projectNavigationDiagnostics;
          throw new BridgeError(
            FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
            failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
          );
        }
        projectScopeVerified = true;
        markCheckpoint('B1', BROWSER_CHECKPOINT_STATUSES.PASS);
        markCheckpoint('B3', BROWSER_CHECKPOINT_STATUSES.PASS);
        await bridge.ensureLoggedIn();
        markCheckpoint('B2', BROWSER_CHECKPOINT_STATUSES.PASS);
        markCheckpoint('B4', BROWSER_CHECKPOINT_STATUSES.PASS);
      } else {
        beforeUrl = await bridge.navigate();
        markCheckpoint('B1', BROWSER_CHECKPOINT_STATUSES.PASS);
      }
      const beforeConversationId = extractConversationIdFromUrl(beforeUrl);
      if (requestedProjectUrl === undefined) {
        await bridge.ensureLoggedIn();
        markCheckpoint('B2', BROWSER_CHECKPOINT_STATUSES.PASS);
        markCheckpoint('B4', BROWSER_CHECKPOINT_STATUSES.PASS);
      }
      let freshConversation;
      try {
        freshConversation = await bridge.createFreshConversation({
          projectUrl: requestedProjectUrl,
          transport,
        });
      } catch (error) {
        markCheckpoint('B5', BROWSER_CHECKPOINT_STATUSES.FAIL, failureClassForCode(error?.code));
        throw error;
      }
      const afterConversationId = extractConversationIdFromUrl(freshConversation.afterUrl);
      if (!afterConversationId && freshConversation.deferred !== true) {
        markCheckpoint('B5', BROWSER_CHECKPOINT_STATUSES.FAIL, 'FRESH_CHAT_CREATION_FAILED');
        throw new BridgeError(
          FAILURE_CODES.FRESH_CHAT_CREATION_FAILED,
          failureMessage(FAILURE_CODES.FRESH_CHAT_CREATION_FAILED),
        );
      }
      markCheckpoint('B5', BROWSER_CHECKPOINT_STATUSES.PASS);
      markCheckpoint(
        'B6',
        afterConversationId ? BROWSER_CHECKPOINT_STATUSES.PASS : BROWSER_CHECKPOINT_STATUSES.DEFERRED,
      );
      if (beforeConversationId && afterConversationId === beforeConversationId) {
        throw new BridgeError(
          FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH,
          failureMessage(FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH),
        );
      }
      // A Project landing's composer creates its `/c/<uuid>` route only when
      // the first prompt is sent.  Keep the expected identity unset until
      // then; the final route check still requires a real conversation UUID.
      expectedConversationId = afterConversationId;
    }

    let baseline = await bridge.waitForAssistantBaseline({
      requireNonEmpty: mode === CONVERSATION_MODES.CONTINUE,
    });
    if (preparedAttachments.files.length > 0) {
      markCheckpoint('B7', BROWSER_CHECKPOINT_STATUSES.DEFERRED);
      attachmentMetadata = attachmentMetadata.map((item) => ({
        ...item,
        upload_status: 'uploading',
      }));
      try {
        await bridge.uploadPreparedAttachments(preparedAttachments.files, {
          mode,
          requestCount: bridge.requestCount,
        });
        attachmentMetadata = attachmentMetadata.map((item) => ({
          ...item,
          upload_status: 'ready',
        }));
        markCheckpoint('B7', BROWSER_CHECKPOINT_STATUSES.PASS);
      } catch (error) {
        attachmentMetadata = attachmentMetadata.map((item) => ({
          ...item,
          upload_status: 'failed',
        }));
        markCheckpoint('B7', BROWSER_CHECKPOINT_STATUSES.FAIL, failureClassForCode(error?.code));
        markCheckpoint('B8', BROWSER_CHECKPOINT_STATUSES.FAIL, failureClassForCode(error?.code));
        throw error;
      }
      const postUploadBaseline = await bridge.waitForAssistantBaseline({
        requireNonEmpty: mode === CONVERSATION_MODES.CONTINUE,
      });
      assertAssistantBaselineUnchanged(baseline, postUploadBaseline);
      baseline = postUploadBaseline;
      markCheckpoint('B8', BROWSER_CHECKPOINT_STATUSES.PASS);
    } else {
      markCheckpoint('B7', BROWSER_CHECKPOINT_STATUSES.SKIP);
      markCheckpoint('B8', BROWSER_CHECKPOINT_STATUSES.SKIP);
    }
    markCheckpoint('B9', BROWSER_CHECKPOINT_STATUSES.DEFERRED);
    if (requestedProjectUrl !== undefined) {
      try {
        if (typeof bridge.verifyProjectScopeBeforeSend === 'function') {
          await bridge.verifyProjectScopeBeforeSend(requestedProjectUrl, {
            allowConversationRoute: mode === CONVERSATION_MODES.CONTINUE,
          });
        } else {
          // Custom bridge factories are test/integration boundaries. Keep the
          // same fail-closed route check even when they do not expose the
          // richer composer verifier.
          const current = safeProjectScopeUrl(
            typeof bridge.currentUrl === 'function' ? bridge.currentUrl() : null,
          );
          const currentIsBoundConversation = mode === CONVERSATION_MODES.CONTINUE
            && isValidProjectConversationUrl(
              typeof bridge.currentUrl === 'function' ? bridge.currentUrl() : null,
              requestedProjectUrl,
              expectedConversationId,
            );
          if (current !== requestedProjectUrl && !currentIsBoundConversation) {
            throw new BridgeError(
              FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
              failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
            );
          }
        }
      } catch (error) {
        projectScopeVerified = false;
        markCheckpoint('B9', BROWSER_CHECKPOINT_STATUSES.FAIL, failureClassForCode(error?.code));
        throw error;
      }
    }
    let responseText;
    try {
      responseText = await bridge.sendOnePrompt(prompt, { baselineSnapshot: baseline });
      markCheckpoint('B9', BROWSER_CHECKPOINT_STATUSES.PASS);
      markCheckpoint('B10', bridge.requestCount > 0 ? BROWSER_CHECKPOINT_STATUSES.PASS : BROWSER_CHECKPOINT_STATUSES.FAIL, bridge.requestCount > 0 ? undefined : 'PROMPT_SUBMISSION_FAILED');
    } catch (error) {
      markCheckpoint('B9', BROWSER_CHECKPOINT_STATUSES.FAIL, failureClassForCode(error?.code));
      throw error;
    }
    markCheckpoint('B11', BROWSER_CHECKPOINT_STATUSES.PASS);
    // The response waiter can complete just before the SPA commits the
    // conversation route.  Wait briefly for that already-created route before
    // checking identity; a fake/injected bridge used by offline tests may not
    // expose the optional settling helper, so it keeps the old direct path.
    verifiedChatUrl = typeof bridge.waitForConversationRoute === 'function'
      ? await bridge.waitForConversationRoute(expectedConversationId)
      : bridge.currentUrl();
    const finalConversationId = extractConversationIdFromUrl(verifiedChatUrl);
    if (!finalConversationId) {
      throw new BridgeError(
        FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH,
        failureMessage(FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH),
      );
    }
    if (expectedConversationId && finalConversationId !== expectedConversationId) {
      throw new BridgeError(
        FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH,
        failureMessage(FAILURE_CODES.CONVERSATION_IDENTITY_MISMATCH),
      );
    }
    if (
      requestedProjectUrl !== undefined
      && !isValidProjectConversationUrl(verifiedChatUrl, requestedProjectUrl, finalConversationId)
    ) {
      projectScopeVerified = false;
      throw new BridgeError(
        FAILURE_CODES.PROJECT_SCOPE_MISMATCH,
        failureMessage(FAILURE_CODES.PROJECT_SCOPE_MISMATCH),
      );
    }
    markCheckpoint('B12', BROWSER_CHECKPOINT_STATUSES.PASS);
    conversationId = finalConversationId;
    conversationValidated = true;
    log(`extracted chars=${responseText.length}`);
    const completed = await writeConsultationArtifacts({
      rootDir: resolvedRoot,
      consultationId,
      createdAt,
      prompt,
      profile,
      mode,
      conversationId,
      parentConsultationId,
      conversationRootConsultationId,
      conversationValidated,
      chatUrl: verifiedChatUrl,
      status: 'complete',
      responseText,
      requestCount: bridge.requestCount,
      attachments: attachmentMetadata,
      contextPack: safeContextPack,
      transport,
      browserCheckpoints,
      projectNavigationDiagnostics,
      projectUrl: requestedProjectUrl,
      projectScopeRequested,
      projectScopeVerified,
      projectScopeEvidence,
    });
    log(`receipt ${completed.receiptPath}`);
    return {
      consultationId,
      responseText,
      requestCount: bridge.requestCount,
      mode,
      projectUrl: requestedProjectUrl,
      projectScopeRequested,
      projectScopeVerified,
      projectScopeEvidence,
      projectNavigationDiagnostics,
      conversationId,
      parentConsultationId,
      conversationRootConsultationId,
      conversationValidated,
      chatUrl: verifiedChatUrl,
      ...completed,
    };
  } catch (error) {
    const failure = asBridgeError(error);
    if (Array.isArray(failure.attachmentMetadata)) attachmentMetadata = failure.attachmentMetadata;
    projectNavigationDiagnostics = safeProjectNavigationDiagnostics(
      failure.projectNavigationDiagnostics || projectNavigationDiagnostics,
    ) || projectNavigationDiagnostics;
    const requestCount = Number.isInteger(bridge?.requestCount) ? bridge.requestCount : 0;
    failure.requestCount = requestCount;
    const failed = await writeConsultationArtifacts({
      rootDir: resolvedRoot,
      consultationId,
      createdAt,
      prompt,
      profile,
      mode,
      conversationId: null,
      parentConsultationId,
      conversationRootConsultationId,
      conversationValidated: false,
      status: requestCount === 0 ? 'failed_before_prompt' : failure.code,
      responseText: '',
      failureCode: failure.code,
      requestCount,
      attachments: attachmentMetadata,
      contextPack: safeContextPack,
      transport,
      browserCheckpoints,
      diagnostics: failure.projectNavigationDiagnostics ? undefined : failure.diagnostics,
      responseForensic: failure.responseForensic,
      projectNavigationDiagnostics,
      projectUrl: requestedProjectUrl,
      projectScopeRequested,
      projectScopeVerified,
      projectScopeEvidence,
    });
    log(`receipt ${failed.receiptPath}`);
    failure.consultationId = consultationId;
    failure.artifacts = failed;
    if (failure.code === FAILURE_CODES.LOGIN_REQUIRED) {
      keepBrowserOpen = true;
      bridge?.releaseForManualLogin();
    }
    throw failure;
  } finally {
    if (!keepBrowserOpen) await bridge?.close();
  }
}

// These failures are all observed before the one prompt budget is consumed.
// The wrapper retries the same semantic intent with a fresh bridge cycle; it
// never retries a send, response wait, continuation, or project decision.
const PRE_PROMPT_RECOVERABLE_FAILURE_CODES = new Set([
  FAILURE_CODES.BRIDGE_TIMEOUT,
  FAILURE_CODES.NETWORK_TRANSIENT,
  FAILURE_CODES.TARGET_CLOSED,
  FAILURE_CODES.CHATGPT_NAVIGATION_FAILED,
  FAILURE_CODES.UNEXPECTED_PAGE_STATE,
  FAILURE_CODES.PROJECT_NAVIGATION_FAILED,
  FAILURE_CODES.PROMPT_INPUT_NOT_FOUND,
  FAILURE_CODES.PROMPT_SEND_FAILED,
  FAILURE_CODES.FRESH_CHAT_CREATION_FAILED,
  FAILURE_CODES.ATTACHMENT_UPLOAD_FAILED,
  FAILURE_CODES.ATTACHMENT_NOT_READY,
]);

function prePromptFailureRequestCount(error) {
  const value = error?.requestCount ?? error?.request_count;
  return Number.isInteger(value) ? value : null;
}

function isPrePromptRecoverableFailure(error) {
  const failure = asBridgeError(error);
  if (prePromptFailureRequestCount(failure) !== 0) return false;
  if (!PRE_PROMPT_RECOVERABLE_FAILURE_CODES.has(failure.code)) return false;
  if (failure.code === FAILURE_CODES.UNEXPECTED_PAGE_STATE) {
    return failure.failureClass === PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP;
  }
  if (failure.code === FAILURE_CODES.PROJECT_NAVIGATION_FAILED) {
    const failureClass = failure.projectNavigationDiagnostics?.failure_class
      || failure.diagnostics?.failure_class
      || failure.failureClass;
    return [
      PROJECT_NAVIGATION_FAILURE_CLASSES.FRONTEND_BOOTSTRAP,
      PROJECT_NAVIGATION_FAILURE_CLASSES.TIMEOUT,
      PROJECT_NAVIGATION_FAILURE_CLASSES.NETWORK,
      PROJECT_NAVIGATION_FAILURE_CLASSES.TARGET_CLOSED,
    ].includes(failureClass);
  }
  return true;
}

function prePromptRecoveryMetadata(failureCodes) {
  return {
    attempted: failureCodes.length > 0,
    cycles: failureCodes.length,
    max_cycles: MAX_PRE_PROMPT_RECOVERY_CYCLES,
    failure_codes: [...failureCodes],
  };
}

/**
 * Run one semantic consultation intent with a hard, infrastructure-only
 * recovery bound. Stage/controller callers invoke this once; only the bridge
 * cycle is repeated while request_count remains zero.
 */
export async function consultOnce(prompt, options = {}) {
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw new TypeError('consultOnce options must be an object');
  }
  const failureCodes = [];
  for (let cycle = 0; cycle <= MAX_PRE_PROMPT_RECOVERY_CYCLES; cycle += 1) {
    try {
      const result = await consultOnceSingle(prompt, options);
      if (failureCodes.length === 0) return result;
      return {
        ...result,
        pre_prompt_recovery: prePromptRecoveryMetadata(failureCodes),
      };
    } catch (error) {
      const failure = asBridgeError(error);
      if (!isPrePromptRecoverableFailure(failure) || cycle >= MAX_PRE_PROMPT_RECOVERY_CYCLES) {
        if (failureCodes.length > 0) {
          failure.prePromptRecovery = prePromptRecoveryMetadata(failureCodes);
        }
        throw failure;
      }
      failureCodes.push(failure.code);
      // A new consultOnceSingle invocation creates a new consultation id,
      // bridge instance, and immutable receipt. It will therefore never
      // resend an already-counted prompt.
    }
  }
  // The loop is structurally total; this branch protects the bound if edited.
  throw new BridgeError(
    FAILURE_CODES.UNEXPECTED_PAGE_STATE,
    'Pre-prompt recovery loop terminated unexpectedly.',
  );
}
