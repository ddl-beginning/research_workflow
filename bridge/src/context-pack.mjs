import crypto from 'node:crypto';
import { execFile as execFileCallback } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

const execFile = promisify(execFileCallback);

export const CONTEXT_PACK_SCHEMA_VERSION = 'context_pack.v1';
export const CONTEXT_PACK_MODES = Object.freeze({
  NORMAL: 'normal',
  FRESH: 'fresh',
});
export const CONTEXT_PACK_STAGING_DIR = 'staging';
export const CONTEXT_MANIFEST_NAME = 'context_manifest.json';
export const MAX_CONTEXT_ATTACHMENTS = 9;
export const MAX_CONTEXT_FILE_BYTES = 8 * 1024 * 1024;
export const MAX_CONTEXT_TEXT_CHARS = 250_000;
export const CONTEXT_PACKET_ID_PATTERN = /^PACK-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export const CONTEXT_PACK_FAILURE_CODES = Object.freeze({
  INVALID: 'CONTEXT_PACK_INVALID',
  ALREADY_EXISTS: 'CONTEXT_PACK_ALREADY_EXISTS',
  SOURCE_NOT_FOUND: 'CONTEXT_PACK_SOURCE_NOT_FOUND',
  SOURCE_ROOT_VIOLATION: 'CONTEXT_PACK_SOURCE_ROOT_VIOLATION',
  SOURCE_NOT_REGULAR_FILE: 'CONTEXT_PACK_SOURCE_NOT_REGULAR_FILE',
  SOURCE_SIZE_EXCEEDED: 'CONTEXT_PACK_SOURCE_SIZE_EXCEEDED',
  DUPLICATE_LOGICAL_FILE: 'CONTEXT_PACK_DUPLICATE_LOGICAL_FILE',
  ATTACHMENT_COUNT_EXCEEDED: 'CONTEXT_PACK_ATTACHMENT_COUNT_EXCEEDED',
  SECRET_REJECTED: 'CONTEXT_PACK_SECRET_REJECTED',
  ABSOLUTE_PATH_REJECTED: 'CONTEXT_PACK_ABSOLUTE_PATH_REJECTED',
  MANIFEST_INVALID: 'CONTEXT_PACK_MANIFEST_INVALID',
  MANIFEST_NOT_FOUND: 'CONTEXT_PACK_MANIFEST_NOT_FOUND',
  INTEGRITY_FAILED: 'CONTEXT_PACK_INTEGRITY_FAILED',
});

const TEXT_EXTENSIONS = new Set([
  '.c', '.cc', '.cpp', '.csv', '.env.example', '.go', '.html', '.ini', '.java',
  '.js', '.json', '.jsx', '.log', '.md', '.mjs', '.py', '.rst', '.sh', '.sql',
  '.tex', '.toml', '.ts', '.tsx', '.txt', '.vue', '.xml', '.yaml', '.yml',
]);

const MANIFEST_ROLES = new Set([
  'stage_context',
  'result',
  'metric',
  'review',
  'source',
  'diff',
]);

const SENSITIVE_COMPONENTS = new Set([
  '.auth',
  'chatgpt-profile',
  'profile',
  'profiles',
  'browser-user-data-dir',
  'user-data-dir',
  'storage-state',
  'storage_state',
  'cookies',
  'cookie',
  'token',
  'tokens',
  'access_token',
  'refresh_token',
  'session_token',
]);

const SECRET_PATTERNS = Object.freeze([
  // Private keys are unambiguous and must never enter an outgoing packet.
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/i,
  // OpenAI, GitHub, Slack, AWS, and common JWT-like bearer credentials.
  /\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b/,
  /\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{16,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b/i,
  // High-confidence assignment names. Short values are included because a
  // local test token or cookie is still not safe to send to a third party.
  /\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|cookie|session(?:[_-]?id)?|github[_-]?token|slack[_-]?token)\s*[:=]\s*(?:["'][^"']+["']|[^\s,;]+)/i,
  /\b(?:set-cookie|document\.cookie)\s*[:=]/i,
  // Quoted JSON keys and JS object keys need an explicit quoted-key form;
  // the assignment rule above intentionally only covers the common textual
  // `api_key = value` shape.
  /(?:["']?(?:api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|password|passwd|secret|cookie|cookies|token|refresh[-_]?token|session[-_]?token|session[-_]?id|github[-_]?token|slack[-_]?token)["']?)\s*[:=]\s*(?:"[^"]+"|'[^']+'|(?!["'])[^\s,;}\]]+)/i,
  // Cookie/storage/session dumps are container-shaped secrets. Require a
  // non-empty value/token field inside the bounded container to avoid
  // rejecting an empty `cookies: []` placeholder.
  /(?:["']?(?:cookies?|cookie[-_]?store|storage[-_]?state|local[-_]?storage|session[-_]?storage|session)["']?)\s*[:=]\s*[\[{][\s\S]{0,8192}?(?:["']?(?:value|token|access[-_]?token|refresh[-_]?token)["']?)\s*[:=]\s*(?:"[^"]+"|'[^']+'|(?!["'])[^\s,;}\]]+)/i,
]);

const ABSOLUTE_PATH_PATTERNS = Object.freeze([
  // The negative look-behind prevents the `s:/` tail of an https:// URL from
  // looking like a Windows drive. The remaining rules cover drive, UNC,
  // file-URI, and generic POSIX absolute paths.
  /(?<![A-Za-z0-9_])[A-Za-z]:[\\/]/m,
  /\\\\[^\\/\s]+[\\/]/m,
  /(?:^|[\s"'(=])\/\/[^\/\s"']+\/[^\/\s"']+/m,
  /\bfile:\/\/\/[^\s"'`)]*/im,
  /(?:^|[\s"'`(=])\/(?![\/\s])[^\\/\s"'`)]*/m,
]);

const FRESH_FORBIDDEN_FIELD_NAMES = new Set([
  'previousRecommendation',
  'previous_recommendation',
  'recommendation',
  'previousChain',
  'previous_chain',
  'previousGptChain',
  'previous_gpt_chain',
  'chainOfThought',
  'chain_of_thought',
  'sunkCostNarrative',
  'sunk_cost_narrative',
  'defenseNarrative',
  'defense_narrative',
  'algorithmDefense',
  'algorithm_defense',
]);

export class ContextPackError extends Error {
  constructor(code, message, details = undefined, cause = undefined) {
    super(message, cause ? { cause } : undefined);
    this.name = 'ContextPackError';
    this.code = code;
    if (details !== undefined) this.details = details;
    this.cause = cause;
  }
}

function fail(code, message, details, cause) {
  return new ContextPackError(code, message, details, cause);
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function compareStrings(left, right) {
  return left === right ? 0 : left < right ? -1 : 1;
}

function sortJsonValue(value) {
  if (Array.isArray(value)) return value.map(sortJsonValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortJsonValue(value[key])]));
  }
  return value;
}

function stableJson(value) {
  return `${JSON.stringify(sortJsonValue(value), null, 2)}\n`;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function text(value, fallback = '') {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== 'string') return String(value);
  return value.trim();
}

function list(value) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value)) return value.map((item) => text(item)).filter(Boolean);
  const normalized = text(value);
  return normalized ? [normalized] : [];
}

function normalizeMode(mode) {
  const normalized = text(mode, CONTEXT_PACK_MODES.NORMAL).toLowerCase();
  if (!Object.values(CONTEXT_PACK_MODES).includes(normalized)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'Context pack mode must be normal or fresh.');
  }
  return normalized;
}

export function isValidContextPacketId(value) {
  return typeof value === 'string' && CONTEXT_PACKET_ID_PATTERN.test(value);
}

export function createContextPacketId(date = new Date(), random = crypto.randomUUID()) {
  const pad = (value) => String(value).padStart(2, '0');
  const timestamp = [
    date.getUTCFullYear(),
    pad(date.getUTCMonth() + 1),
    pad(date.getUTCDate()),
    '-',
    pad(date.getUTCHours()),
    pad(date.getUTCMinutes()),
    pad(date.getUTCSeconds()),
  ].join('');
  return `PACK-${timestamp}-${random.slice(0, 8)}`;
}

function normalizeRelative(value, fieldName = 'relative path') {
  if (typeof value !== 'string' || !value.trim()) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `${fieldName} must be a non-empty relative path.`);
  }
  const normalized = value.trim().replaceAll('\\', '/');
  if (
    normalized.startsWith('/')
    || /^[A-Za-z]:\//.test(normalized)
    || normalized.startsWith('//')
    || normalized.split('/').some((part) => part === '..' || part === '')
  ) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `${fieldName} must stay within the packet.`);
  }
  return normalized;
}

function isPathInside(root, target) {
  const relative = path.relative(root, target);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function samePath(left, right) {
  const normalizedLeft = path.normalize(left);
  const normalizedRight = path.normalize(right);
  return process.platform === 'win32'
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

async function ensureDirectoryWithoutSymlink(directoryPath) {
  try {
    const stat = await fs.lstat(directoryPath);
    if (stat.isSymbolicLink()) {
      throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context staging boundary cannot contain a symlink.');
    }
    if (!stat.isDirectory()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context staging boundary must be a directory.');
  } catch (error) {
    if (error instanceof ContextPackError) throw error;
    if (error?.code !== 'ENOENT') throw error;
    await fs.mkdir(directoryPath, { recursive: false, mode: 0o700 });
  }
}

async function assertCanonicalStagingBoundary({ rootDir, packetId, stagingDir = undefined, allowMissingPacket = false, createParents = false }) {
  const realRoot = await fs.realpath(path.resolve(rootDir)).catch((error) => {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context pack root could not be canonicalized.', undefined, error);
  });
  const consultationsPath = path.join(realRoot, '.consultations');
  const stagingRootPath = path.join(consultationsPath, CONTEXT_PACK_STAGING_DIR);
  if (createParents) {
    await ensureDirectoryWithoutSymlink(consultationsPath);
    await ensureDirectoryWithoutSymlink(stagingRootPath);
  }
  let realStagingRoot;
  try {
    realStagingRoot = await fs.realpath(stagingRootPath);
  } catch (error) {
    if (error?.code === 'ENOENT') throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_NOT_FOUND, 'The context staging root was not found.', undefined, error);
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context staging root could not be verified.', undefined, error);
  }
  if (!samePath(realStagingRoot, stagingRootPath) || !isPathInside(realRoot, realStagingRoot)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context staging root escaped the project root.');
  }
  const packetPath = path.join(realStagingRoot, packetId);
  let packetStat;
  try {
    packetStat = await fs.lstat(packetPath);
  } catch (error) {
    if (error?.code === 'ENOENT' && allowMissingPacket) {
      return { rootDir: realRoot, stagingRoot: realStagingRoot, packetDir: packetPath };
    }
    if (error?.code === 'ENOENT') throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_NOT_FOUND, 'The context packet directory was not found.', undefined, error);
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet directory could not be inspected.', undefined, error);
  }
  if (packetStat.isSymbolicLink()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet directory cannot be a symlink.');
  if (!packetStat.isDirectory()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet path must be a directory.');
  const realPacket = await fs.realpath(packetPath).catch((error) => {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet directory could not be canonicalized.', undefined, error);
  });
  if (!samePath(realPacket, packetPath) || !isPathInside(realStagingRoot, realPacket)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet directory escaped the staging root.');
  }
  if (stagingDir !== undefined && !samePath(path.resolve(stagingDir), packetPath)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context pack staging path does not match its canonical packet boundary.');
  }
  return { rootDir: realRoot, stagingRoot: realStagingRoot, packetDir: realPacket };
}

function isSensitivePath(filePath) {
  const components = path.normalize(filePath).split(/[\\/]+/).filter(Boolean).map((part) => part.toLowerCase());
  if (components.some((part) => SENSITIVE_COMPONENTS.has(part))) return true;
  if (components.some((part) => /^(?:oauth[-_ ]?)?(?:access|refresh|session)?[-_ ]?tokens?$/.test(part))) return true;
  const basename = components.at(-1) || '';
  if (components.some((part) => /^(?:browser|chrome|chromium)[-_ ]?profile(?:[-_ ].*)?$/.test(part))) return true;
  return (
    /^\.env(?:\..*)?$/.test(basename)
    || /\.(?:pem|key|p12|pfx)$/i.test(basename)
    || /^id_(?:rsa|ed25519)$/.test(basename)
    || /(?:private[-_ ]?key|credential|secret)/i.test(basename)
    || /(?:token|cookie|session|password)/i.test(basename)
    || /^(?:cookies?|login data|preferences|web data|history|session storage|local storage)$/i.test(basename)
  );
}

export function findSecretPattern(value) {
  if (typeof value !== 'string' || !value) return null;
  for (const pattern of SECRET_PATTERNS) {
    const match = value.match(pattern);
    if (match) return { pattern: pattern.source, index: match.index ?? 0 };
  }
  return null;
}

export function assertNoSecrets(value, label = 'context pack content') {
  const match = findSecretPattern(value);
  if (match) {
    throw fail(
      CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED,
      `High-confidence secret material was detected in ${label}; the context pack was rejected without redaction.`,
      { label },
    );
  }
  return true;
}

export function findAbsolutePath(value) {
  if (typeof value !== 'string' || !value) return null;
  for (const pattern of ABSOLUTE_PATH_PATTERNS) {
    const match = value.match(pattern);
    if (match) return { index: match.index ?? 0 };
  }
  return null;
}

export function assertNoAbsolutePaths(value, label = 'GPT-visible context') {
  if (findAbsolutePath(value)) {
    throw fail(
      CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED,
      `An absolute local path was detected in ${label}; use a repository-relative path instead.`,
      { label },
    );
  }
  return true;
}

function ensureTextSafe(value, label) {
  const normalized = text(value);
  if (normalized.length > MAX_CONTEXT_TEXT_CHARS) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `${label} is too large for a bounded context pack.`);
  }
  assertNoSecrets(normalized, label);
  assertNoAbsolutePaths(normalized, label);
  return normalized;
}

function bulletList(values) {
  return values.length > 0 ? values.map((value) => `- ${value}`).join('\n') : '- None supplied.';
}

function renderLatestResultValue(value, label) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'object') {
    let serialized;
    try {
      serialized = stableJson(value).trimEnd();
    } catch (error) {
      throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `${label} must be JSON-serializable.`, undefined, error);
    }
    return ensureTextSafe(serialized, label);
  }
  return ensureTextSafe(value, label);
}

function renderLatestResultList(value, label) {
  if (value === undefined || value === null) return [];
  const entries = Array.isArray(value) ? value : [value];
  return entries
    .map((item, index) => renderLatestResultValue(item, `${label}[${index}]`))
    .filter(Boolean);
}

function normalizeDecision(value) {
  if (typeof value === 'string') {
    return { text: ensureTextSafe(value, 'previous decision'), kind: 'decision', source: 'codex' };
  }
  if (!value || typeof value !== 'object') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'Previous decisions must be strings or objects.');
  }
  const item = {
    text: ensureTextSafe(value.text ?? value.summary ?? '', 'previous decision'),
    kind: text(value.kind, 'decision').toLowerCase(),
    source: text(value.source, 'codex').toLowerCase(),
  };
  if (!item.text) return null;
  return item;
}

function decisionsForMode(values, mode) {
  return values
    .map(normalizeDecision)
    .filter(Boolean)
    .filter((item) => mode !== CONTEXT_PACK_MODES.FRESH || (
      item.kind !== 'gpt_recommendation'
      && item.kind !== 'recommendation'
      && item.source !== 'gpt'
    ));
}

function assertFreshInputContract(input, latestResult, previousRelevantDecisions, mode) {
  if (mode !== CONTEXT_PACK_MODES.FRESH) return;
  const candidates = [
    ['context pack input', input],
    ['latestResult', latestResult],
    ...((Array.isArray(previousRelevantDecisions) ? previousRelevantDecisions : []).map((item, index) => [`previousRelevantDecisions[${index}]`, item])),
  ];
  for (const [label, candidate] of candidates) {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
    const forbidden = Object.keys(candidate).find((key) => FRESH_FORBIDDEN_FIELD_NAMES.has(key));
    if (forbidden) {
      throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `FRESH context cannot receive ${label}.${forbidden}; supply only structured current evidence.`);
    }
  }
}

function renderStageContext({
  projectGoal,
  currentStageGoal,
  userVisibleGoal,
  establishedFacts,
  currentMethod,
  currentBlocker,
  protectedForbiddenScope,
  hardConstraints,
  previousRelevantDecisions,
  mode,
}) {
  const decisions = decisionsForMode(previousRelevantDecisions, mode);
  const sections = [
    '# Project Goal',
    ensureTextSafe(projectGoal, 'project goal') || 'Not supplied.',
    '',
    '# Current Stage Goal',
    ensureTextSafe(currentStageGoal, 'current stage goal') || 'Not supplied.',
    '',
    '# User-visible Goal',
    ensureTextSafe(userVisibleGoal, 'user-visible goal') || 'Not supplied.',
    '',
    '# Established Facts',
    bulletList(list(establishedFacts).map((item) => ensureTextSafe(item, 'established fact'))),
    '',
    '# Current Method',
    ensureTextSafe(currentMethod, 'current method') || 'Not supplied.',
    '',
    '# Current Blocker',
    ensureTextSafe(currentBlocker, 'current blocker') || 'Not supplied.',
    '',
    '# Protected / Forbidden Scope',
    bulletList(list(protectedForbiddenScope).map((item) => ensureTextSafe(item, 'protected scope'))),
    '',
    '# Hard Constraints',
    bulletList(list(hardConstraints).map((item) => ensureTextSafe(item, 'hard constraint'))),
    '',
    '# Relevant Previous Decisions',
    decisions.length > 0
      ? decisions.map((item) => `- [${item.kind}; ${item.source}] ${item.text}`).join('\n')
      : '- None supplied for this review mode.',
    '',
  ];
  const output = `${sections.join('\n')}`;
  assertNoSecrets(output, 'STAGE_CONTEXT.md');
  assertNoAbsolutePaths(output, 'STAGE_CONTEXT.md');
  return output;
}

function renderLatestResult(latestResult = {}) {
  if (typeof latestResult === 'string') latestResult = { actualWork: latestResult };
  if (!latestResult || typeof latestResult !== 'object') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'latestResult must be a string or object.');
  }
  const output = [
    '# Latest Result',
    '',
    '## What Codex actually did',
    renderLatestResultValue(
      latestResult.actualWork ?? latestResult.work ?? latestResult.codexWork ?? latestResult.summary,
      'latest result work',
    ) || 'Not supplied.',
    '',
    '## What was tested',
    bulletList(renderLatestResultList(latestResult.tested ?? latestResult.tests, 'latest result test')),
    '',
    '## What succeeded',
    bulletList(renderLatestResultList(latestResult.success ?? latestResult.successes, 'latest result success')),
    '',
    '## What failed',
    bulletList(renderLatestResultList(latestResult.failure ?? latestResult.failures, 'latest result failure')),
    '',
    '## Change relative to the previous result',
    renderLatestResultValue(latestResult.change ?? latestResult.delta ?? latestResult.changed, 'latest result change') || 'Not supplied.',
    '',
    '## Why GPT consultation is needed now',
    renderLatestResultValue(latestResult.whyConsult ?? latestResult.reason, 'latest result consultation reason') || 'Not supplied.',
    '',
    '## Local detail references',
    bulletList(renderLatestResultList(latestResult.localReferences ?? latestResult.detailsReference, 'local detail reference')),
    '',
    '## Evidence references',
    bulletList(renderLatestResultList(latestResult.evidence_refs ?? latestResult.evidenceRefs, 'latest result evidence reference')),
    '',
  ].join('\n');
  assertNoSecrets(output, 'LATEST_RESULT.md');
  assertNoAbsolutePaths(output, 'LATEST_RESULT.md');
  return output;
}

function renderSourceContext(sourceContext) {
  if (sourceContext === undefined || sourceContext === null) return null;
  const entries = Array.isArray(sourceContext) ? sourceContext : [sourceContext];
  const rendered = ['# Source Context', '', 'Only the bounded excerpts below were explicitly selected for this review.', ''];
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'sourceContext entries must be objects.');
    const filePath = ensureTextSafe(entry.filePath ?? entry.relativePath ?? '', 'source context path');
    const relativePath = normalizeRelative(filePath, 'source context path');
    const functions = list(entry.relevantFunctions ?? entry.functions).map((item) => ensureTextSafe(item, 'source function'));
    const excerpt = ensureTextSafe(entry.excerpt ?? entry.content ?? '', 'source excerpt');
    const whyRelevant = ensureTextSafe(entry.whyRelevant ?? entry.reason, 'source context rationale');
    rendered.push(`## ${relativePath}`);
    rendered.push(`Relevant functions: ${functions.length > 0 ? functions.join(', ') : 'Not supplied.'}`);
    rendered.push(`Why relevant: ${whyRelevant || 'Not supplied.'}`);
    rendered.push('');
    rendered.push('```');
    rendered.push(excerpt || 'No excerpt supplied.');
    rendered.push('```');
    rendered.push('');
  }
  const output = `${rendered.join('\n')}`;
  assertNoSecrets(output, 'SOURCE_CONTEXT.md');
  assertNoAbsolutePaths(output, 'SOURCE_CONTEXT.md');
  return output;
}

function normalizeGitChangedFiles(value) {
  if (!Array.isArray(value)) return [];
    return [...new Set(value.map((item) => normalizeRelative(String(item), 'changed file path')))].sort(compareStrings);
}

function safeRepoRootId(realRoot) {
  return `repo-${sha256(realRoot).slice(0, 16)}`;
}

async function readGitMetadata(rootDir, override) {
  if (override !== undefined) {
    if (!override || typeof override !== 'object') throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'gitMetadata must be an object.');
    const rootIdentifier = text(override.rootIdentifier ?? override.root_id, safeRepoRootId(rootDir));
    assertNoSecrets(rootIdentifier, 'git root identifier');
    assertNoAbsolutePaths(rootIdentifier, 'git root identifier');
    return {
      rootIdentifier,
      commit: override.commit === null || override.commit === undefined ? null : text(override.commit),
      dirty: override.dirty === null || override.dirty === undefined ? null : Boolean(override.dirty),
      changedFiles: normalizeGitChangedFiles(override.changedFiles ?? override.changed_files),
    };
  }

  const fallback = {
    rootIdentifier: safeRepoRootId(rootDir),
    commit: null,
    dirty: null,
    changedFiles: [],
  };
  try {
    const topLevel = (await execFile('git', ['-C', rootDir, 'rev-parse', '--show-toplevel'], { windowsHide: true })).stdout.trim();
    const realTop = await fs.realpath(topLevel);
    const commit = (await execFile('git', ['-C', rootDir, 'rev-parse', 'HEAD'], { windowsHide: true })).stdout.trim();
    const status = (await execFile('git', ['-C', rootDir, 'status', '--short'], { windowsHide: true })).stdout;
    const changedFiles = status
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => line.length > 3 ? line.slice(3).trim() : line.trim())
      .map((value) => value.includes(' -> ') ? value.split(' -> ').at(-1) : value)
      .map((value) => normalizeRelative(value.replace(/^"|"$/g, ''), 'changed file path'))
      .sort(compareStrings);
    return {
      rootIdentifier: safeRepoRootId(realTop),
      commit: /^[0-9a-f]{40}$/i.test(commit) ? commit : null,
      dirty: status.trim().length > 0,
      changedFiles: [...new Set(changedFiles)],
    };
  } catch {
    return fallback;
  }
}

function normalizeLogicalName(value) {
  const normalized = normalizeRelative(value, 'logical_name');
  if (normalized.includes('/') && normalized.startsWith('..')) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'logical_name must stay within the packet.');
  }
  return normalized;
}

function extensionOf(filePath) {
  const lower = filePath.toLowerCase();
  for (const extension of TEXT_EXTENSIONS) {
    if (lower.endsWith(extension)) return extension;
  }
  return path.extname(lower);
}

function isTextFile(filePath, mediaType = undefined) {
  if (typeof mediaType === 'string' && mediaType.startsWith('text/')) return true;
  return TEXT_EXTENSIONS.has(extensionOf(filePath));
}

function safetyScanText(bytes, textFile) {
  const decoded = bytes.toString('utf8');
  return textFile
    ? decoded
    : Array.from(decoded.matchAll(/[\x20-\x7E]{8,}/g), (match) => match[0]).join('\n');
}

async function readExplicitSource(descriptor, { rootDir, sourceRoots }) {
  if (!descriptor || typeof descriptor !== 'object') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'Evidence entries must be objects.');
  }
  const sourcePath = descriptor.sourcePath ?? descriptor.source_path;
  const hasContent = descriptor.content !== undefined || descriptor.text !== undefined;
  if (sourcePath !== undefined && hasContent) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'Evidence cannot specify both sourcePath and content.');
  }
  const logicalName = normalizeLogicalName(descriptor.logicalName ?? descriptor.logical_name ?? descriptor.stagedName ?? descriptor.staged_name ?? path.basename(String(sourcePath || 'evidence.txt')));
  const role = text(descriptor.role, 'source').toLowerCase();
  if (!MANIFEST_ROLES.has(role)) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `Unsupported evidence role: ${role}.`);
  const stagedName = normalizeRelative(descriptor.stagedName ?? descriptor.staged_name ?? `evidence/${logicalName}`, 'staged path');

  if (hasContent) {
    const value = String(descriptor.content ?? descriptor.text ?? '');
    if (Buffer.byteLength(value, 'utf8') > MAX_CONTEXT_FILE_BYTES) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_SIZE_EXCEEDED, `${logicalName} exceeds the context file limit.`);
    // Inline content is already in the outgoing packet regardless of its
    // declared media type. Binary-looking logical names must not bypass the
    // same secret/path checks as Markdown or JSON.
    assertNoSecrets(value, logicalName);
    assertNoAbsolutePaths(value, logicalName);
    const safeValue = isTextFile(logicalName, descriptor.mediaType) ? ensureTextSafe(value, logicalName) : value;
    const sourceRelativePath = descriptor.sourceRelativePath ?? descriptor.source_relative_path ?? null;
    if (sourceRelativePath !== null) ensureTextSafe(String(sourceRelativePath), 'source relative path');
    return {
      logicalName,
      role,
      stagedPath: stagedName,
      sourceRelativePath,
      bytes: Buffer.from(safeValue, 'utf8'),
      mediaType: descriptor.mediaType ?? null,
    };
  }

  if (typeof sourcePath !== 'string' || !sourcePath.trim()) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `${logicalName} needs sourcePath or content.`);
  }
  const requested = path.resolve(rootDir, sourcePath);
  if (isSensitivePath(requested)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED, `Sensitive source path ${logicalName} is not permitted in a context pack.`);
  }
  let realPath;
  try {
    realPath = await fs.realpath(requested);
  } catch (error) {
    if (error?.code === 'ENOENT') throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_NOT_FOUND, `Evidence source ${logicalName} was not found.`, undefined, error);
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `Evidence source ${logicalName} could not be canonicalized.`, undefined, error);
  }
  // Re-check the canonical target. A harmless-looking alias may resolve into
  // .auth, a browser profile, Cookies/Login Data, or another credential
  // location. Do this before stat/read so no sensitive target is staged.
  if (isSensitivePath(realPath)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED, `Evidence source ${logicalName} resolves to a protected profile or credential path.`);
  }
  const root = sourceRoots.find((candidate) => isPathInside(candidate, realPath));
  if (!root) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, `Evidence source ${logicalName} is outside the explicit evidence roots.`);
  const stat = await fs.stat(realPath);
  if (!stat.isFile()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_NOT_REGULAR_FILE, `Evidence source ${logicalName} is not a regular file.`);
  if (stat.size > MAX_CONTEXT_FILE_BYTES) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_SIZE_EXCEEDED, `${logicalName} exceeds the context file limit.`);
  const bytes = await fs.readFile(realPath);
  // Source files are outgoing content even when their extension or declared
  // media type says binary. Scan printable ASCII runs in binary payloads so
  // embedded credentials/paths are still rejected, but do not decode the
  // complete compressed byte stream: arbitrary bytes can form false
  // Windows-drive sequences such as `C:/` by chance. Text files still
  // receive the full character-limit validation below.
  const textFile = isTextFile(realPath, descriptor.mediaType);
  const decoded = bytes.toString('utf8');
  const safetyScan = safetyScanText(bytes, textFile);
  assertNoSecrets(safetyScan, logicalName);
  assertNoAbsolutePaths(safetyScan, logicalName);
  if (textFile) ensureTextSafe(decoded, logicalName);
  const relative = path.relative(rootDir, realPath);
  return {
    logicalName,
    role,
    stagedPath: stagedName,
    sourceRelativePath: relative && !relative.startsWith('..') && !path.isAbsolute(relative) ? relative.split(path.sep).join('/') : path.basename(realPath),
    bytes,
    mediaType: descriptor.mediaType ?? null,
    realPath,
  };
}

function normalizeEvidenceDescriptors(evidence) {
  if (evidence === undefined) return [];
  if (!Array.isArray(evidence)) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'evidence must be an explicit array.');
  return evidence.slice();
}

function deduplicateEvidence(files) {
  const byLogical = new Map();
  const result = [];
  for (const file of files.sort((left, right) => (
    compareStrings(left.logicalName, right.logicalName)
    || compareStrings(left.role, right.role)
    || compareStrings(left.stagedPath, right.stagedPath)
  ))) {
    const existing = byLogical.get(file.logicalName);
    const digest = sha256(file.bytes);
    if (existing) {
      if (existing.digest !== digest || existing.role !== file.role) {
        throw fail(CONTEXT_PACK_FAILURE_CODES.DUPLICATE_LOGICAL_FILE, `Logical file ${file.logicalName} was selected more than once with conflicting content.`);
      }
      continue;
    }
    const item = { ...file, digest };
    byLogical.set(file.logicalName, item);
    result.push(item);
  }
  return result;
}

function fileDescriptor({ logicalName, stagedPath, role, bytes, sourceRelativePath = null, mediaType = null }) {
  return {
    logicalName,
    stagedPath,
    role,
    sourceRelativePath,
    mediaType,
    bytes,
    digest: sha256(bytes),
  };
}

function normalizePreviousManifest(previousPack) {
  if (!previousPack) return null;
  const manifest = previousPack.manifest ?? previousPack;
  if (!validateContextManifest(manifest)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'previousPack does not contain a valid context manifest.');
  }
  return manifest;
}

function createManifestFiles(files) {
  return files
    .slice()
    .sort((left, right) => compareStrings(left.logicalName, right.logicalName) || compareStrings(left.stagedPath, right.stagedPath))
    .map((file) => ({
      logical_name: file.logicalName,
      relative_staged_path: file.stagedPath,
      sha256: file.digest,
      bytes: file.bytes.byteLength,
      role: file.role,
      ...(file.sourceRelativePath ? { source_relative_path: normalizeRelative(file.sourceRelativePath, 'source relative path') } : {}),
    }));
}

function manifestForPack({ packetId, mode, createdAt, git, files, reusedFiles, packHash }) {
  return {
    schema_version: CONTEXT_PACK_SCHEMA_VERSION,
    packet_id: packetId,
    mode,
    source_git_root_id: git.rootIdentifier,
    source_git_commit: git.commit,
    source_git_dirty: git.dirty,
    source_git_changed_files: git.changedFiles,
    created_at: createdAt,
    files: createManifestFiles(files),
    attachment_count: files.length,
    ...(reusedFiles.length > 0 ? { reused_files: reusedFiles } : {}),
    pack_sha256: packHash,
  };
}

function normalizedManifestFile(item) {
  return {
    logical_name: item.logical_name,
    relative_staged_path: item.relative_staged_path,
    sha256: item.sha256,
    bytes: item.bytes,
    role: item.role,
    ...(item.source_relative_path === undefined ? {} : { source_relative_path: item.source_relative_path }),
  };
}

function normalizedManifestHashInput(manifest) {
  const files = (manifest.files || [])
    .map(normalizedManifestFile)
    .sort((left, right) => compareStrings(left.logical_name, right.logical_name) || compareStrings(left.relative_staged_path, right.relative_staged_path));
  const reusedFiles = (manifest.reused_files || [])
    .map((item) => ({
      logical_name: item.logical_name,
      source_packet_id: item.source_packet_id,
      relative_staged_path: item.relative_staged_path,
      sha256: item.sha256,
      ...(item.role === undefined ? {} : { role: item.role }),
    }))
    .sort((left, right) => compareStrings(left.logical_name, right.logical_name) || compareStrings(left.source_packet_id, right.source_packet_id));
  return {
    schema_version: manifest.schema_version,
    mode: manifest.mode,
    source_git_root_id: manifest.source_git_root_id,
    source_git_commit: manifest.source_git_commit,
    source_git_dirty: manifest.source_git_dirty,
    source_git_changed_files: [...(manifest.source_git_changed_files || [])].sort(compareStrings),
    files,
    reused_files: reusedFiles,
  };
}

export function computeContextPackHash(manifest) {
  if (!manifest || typeof manifest !== 'object') throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'Cannot hash a missing context manifest.');
  return sha256(Buffer.from(canonicalJson(normalizedManifestHashInput(manifest)), 'utf8'));
}

export function validateContextManifest(manifest) {
  if (!manifest || typeof manifest !== 'object') return false;
  try {
    assertNoAbsolutePaths(JSON.stringify(manifest), CONTEXT_MANIFEST_NAME);
  } catch {
    return false;
  }
  if (manifest.schema_version !== CONTEXT_PACK_SCHEMA_VERSION) return false;
  if (!isValidContextPacketId(manifest.packet_id)) return false;
  if (!Object.values(CONTEXT_PACK_MODES).includes(manifest.mode)) return false;
  if (!(manifest.source_git_commit === null || typeof manifest.source_git_commit === 'string')) return false;
  if (!(manifest.source_git_dirty === null || typeof manifest.source_git_dirty === 'boolean')) return false;
  if (typeof manifest.source_git_root_id !== 'string' || !manifest.source_git_root_id) return false;
  if (!Array.isArray(manifest.source_git_changed_files)) return false;
  if (!manifest.source_git_changed_files.every((item) => {
    try { normalizeRelative(item, 'changed file path'); return true; } catch { return false; }
  })) return false;
  if (typeof manifest.created_at !== 'string' || !manifest.created_at) return false;
  if (!Array.isArray(manifest.files) || manifest.files.length > MAX_CONTEXT_ATTACHMENTS) return false;
  if (!Number.isInteger(manifest.attachment_count) || manifest.attachment_count !== manifest.files.length) return false;
  if (!manifest.files.every((item) => (
    item
    && typeof item === 'object'
    && typeof item.logical_name === 'string'
    && typeof item.relative_staged_path === 'string'
    && /^[0-9a-f]{64}$/i.test(item.sha256)
    && (item.role === undefined || MANIFEST_ROLES.has(item.role))
    && Number.isInteger(item.bytes)
    && item.bytes >= 0
    && MANIFEST_ROLES.has(item.role)
    && (() => { try { normalizeRelative(item.relative_staged_path, 'relative_staged_path'); return true; } catch { return false; } })()
    && (item.source_relative_path === undefined || (() => { try { normalizeRelative(item.source_relative_path, 'source_relative_path'); return true; } catch { return false; } })())
  ))) return false;
  if (manifest.reused_files !== undefined && (!Array.isArray(manifest.reused_files) || !manifest.reused_files.every((item) => (
    item
    && typeof item.logical_name === 'string'
    && typeof item.source_packet_id === 'string'
    && isValidContextPacketId(item.source_packet_id)
    && typeof item.relative_staged_path === 'string'
    && /^[0-9a-f]{64}$/i.test(item.sha256)
    && (() => { try { normalizeRelative(item.relative_staged_path, 'reused relative_staged_path'); return true; } catch { return false; } })()
  )))) return false;
  if (typeof manifest.pack_sha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(manifest.pack_sha256)) return false;
  try {
    return computeContextPackHash(manifest) === manifest.pack_sha256;
  } catch {
    return false;
  }
}

export function contextPackReceiptMetadata(pack) {
  if (!pack || typeof pack !== 'object') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'contextPack must be an object.');
  }
  const manifest = pack.manifest;
  if (!validateContextManifest(manifest)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack manifest is invalid.');
  }
  if (manifest.packet_id !== pack.packetId) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack packet id does not match its manifest.');
  }
  if (typeof pack.manifestSha256 !== 'string' || !/^[0-9a-f]{64}$/i.test(pack.manifestSha256)) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack manifest hash is invalid.');
  }
  if (typeof pack.packHash !== 'string' || pack.packHash !== manifest.pack_sha256) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, 'contextPack pack hash does not match its manifest.');
  }
  return {
    packet_id: manifest.packet_id,
    mode: manifest.mode,
    relative_manifest_path: `${CONTEXT_PACK_STAGING_DIR}/${manifest.packet_id}/${CONTEXT_MANIFEST_NAME}`,
    manifest_sha256: pack.manifestSha256,
    pack_sha256: manifest.pack_sha256,
    attachment_count: manifest.attachment_count,
  };
}

/**
 * Resolve the one directory that may contain attachments for a pack built by
 * this module.  A pack may be built in a caller-owned disposable workspace
 * (for example the supervisor's .tmp directory), so the bridge cannot assume
 * that its process root contains the files.  The returned path is still
 * narrow: it is the canonical `.consultations/staging` directory verified by
 * the pack boundary, never the caller's whole workspace.
 */
export async function resolveContextPackAttachmentRoot(pack) {
  const provenance = contextPackReceiptMetadata(pack);
  if (typeof pack.stagingDir !== 'string' || typeof pack.rootDir !== 'string' || !pack.rootDir.trim()) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack local staging paths are missing.');
  }
  const boundary = await assertCanonicalStagingBoundary({
    rootDir: pack.rootDir,
    packetId: provenance.packet_id,
    stagingDir: pack.stagingDir,
  });
  return boundary.stagingRoot;
}

export async function validateContextPackForConsult(pack, { expectedConversationMode = undefined } = {}) {
  const provenance = contextPackReceiptMetadata(pack);
  if (provenance.mode === CONTEXT_PACK_MODES.FRESH && expectedConversationMode && expectedConversationMode !== 'fresh') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'A fresh context pack must use a fresh ChatGPT conversation.');
  }
  if (typeof pack.manifestPath !== 'string' || typeof pack.stagingDir !== 'string') {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack local paths are missing.');
  }
  if (typeof pack.rootDir !== 'string' || !pack.rootDir.trim()) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack rootDir is missing.');
  }
  const boundary = await assertCanonicalStagingBoundary({
    rootDir: pack.rootDir,
    packetId: provenance.packet_id,
    stagingDir: pack.stagingDir,
  });
  const realStaging = boundary.packetDir;
  const realManifest = await fs.realpath(pack.manifestPath).catch((error) => {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_NOT_FOUND, 'The context pack manifest was not found.', undefined, error);
  });
  if (!isPathInside(realStaging, realManifest) || path.basename(realManifest) !== CONTEXT_MANIFEST_NAME) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'The context pack manifest path escaped its staging directory.');
  }
  const diskManifestText = await fs.readFile(realManifest, 'utf8');
  assertNoSecrets(diskManifestText, CONTEXT_MANIFEST_NAME);
  assertNoAbsolutePaths(diskManifestText, CONTEXT_MANIFEST_NAME);
  let diskManifest;
  try { diskManifest = JSON.parse(diskManifestText); } catch (error) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'The context pack manifest is not valid JSON.', undefined, error);
  }
  if (!validateContextManifest(diskManifest) || diskManifest.packet_id !== provenance.packet_id) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'The on-disk context manifest failed schema validation.');
  }
  if (
    diskManifest.schema_version !== pack.manifest.schema_version
    || diskManifest.packet_id !== pack.manifest.packet_id
    || diskManifest.mode !== pack.manifest.mode
    || canonicalJson(diskManifest) !== canonicalJson(pack.manifest)
  ) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, 'The on-disk context manifest differs from the selected in-memory packet.');
  }
  if (diskManifest.pack_sha256 !== provenance.pack_sha256 || computeContextPackHash(diskManifest) !== diskManifest.pack_sha256) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, 'The context manifest pack hash is forged or inconsistent.');
  }
  if (sha256(Buffer.from(diskManifestText, 'utf8')) !== pack.manifestSha256) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, 'The on-disk context manifest hash changed after staging.');
  }
  const attachmentPaths = [];
  const verifiedFiles = [];
  for (const item of diskManifest.files) {
    const relative = normalizeRelative(item.relative_staged_path, 'relative_staged_path');
    const candidate = path.resolve(realStaging, relative);
    if (!isPathInside(realStaging, candidate)) throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'A staged attachment escaped its packet directory.');
    const realFile = await fs.realpath(candidate).catch((error) => {
      throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_NOT_FOUND, `Staged file ${item.logical_name} was not found.`, undefined, error);
    });
    if (!isPathInside(realStaging, realFile)) throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'A staged attachment symlink escaped its packet directory.');
    const bytes = await fs.readFile(realFile);
    if (item.bytes > MAX_CONTEXT_FILE_BYTES) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_SIZE_EXCEEDED, `Staged file ${item.logical_name} exceeds the context file limit.`);
    if (bytes.byteLength !== item.bytes || sha256(bytes) !== item.sha256) {
      throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, `Staged file ${item.logical_name} failed integrity verification.`);
    }
    // Re-scan every bounded staged payload immediately before upload. A
    // binary extension (for example PNG/BIN) is not a trust boundary: an
    // attacker could alter bytes, synchronize the item and pack hashes, and
    // otherwise bypass a text-only check. UTF-8 decoding is intentionally
    // conservative here; high-confidence ASCII credentials and local paths
    // remain visible in binary containers and false positives fail closed.
    const textFile = isTextFile(item.relative_staged_path);
    const decoded = bytes.toString('utf8');
    const safetyScan = safetyScanText(bytes, textFile);
    assertNoSecrets(safetyScan, item.logical_name);
    assertNoAbsolutePaths(safetyScan, item.logical_name);
    if (textFile) ensureTextSafe(decoded, item.logical_name);
    verifiedFiles.push({ ...item, bytes: bytes.byteLength, sha256: sha256(bytes) });
    attachmentPaths.push(realFile);
  }
  const verifiedManifest = { ...diskManifest, files: verifiedFiles };
  if (computeContextPackHash(verifiedManifest) !== diskManifest.pack_sha256) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED, 'The context pack hash does not match verified staged file bytes.');
  }
  const expectedPaths = Array.isArray(pack.attachmentPaths) ? pack.attachmentPaths.map((value) => path.resolve(value)) : attachmentPaths;
  if (expectedPaths.length !== attachmentPaths.length || expectedPaths.some((value, index) => path.resolve(value) !== path.resolve(attachmentPaths[index]))) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'contextPack attachment paths do not match its manifest.');
  }
  return { ...pack, manifest: diskManifest, attachmentPaths, packHash: diskManifest.pack_sha256, provenance };
}

export async function loadContextPack({ rootDir, packetId }) {
  if (!isValidContextPacketId(packetId)) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'packetId is invalid.');
  const resolvedRoot = await fs.realpath(path.resolve(rootDir)).catch((error) => {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context pack root could not be canonicalized.', undefined, error);
  });
  const boundary = await assertCanonicalStagingBoundary({
    rootDir: resolvedRoot,
    packetId,
  });
  const stagingDir = boundary.packetDir;
  const manifestPath = path.join(stagingDir, CONTEXT_MANIFEST_NAME);
  let manifest;
  try { manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8')); } catch (error) {
    if (error?.code === 'ENOENT') throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_NOT_FOUND, 'The context pack manifest was not found.', undefined, error);
    throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'The context pack manifest could not be parsed.', undefined, error);
  }
  if (!validateContextManifest(manifest)) throw fail(CONTEXT_PACK_FAILURE_CODES.MANIFEST_INVALID, 'The context pack manifest failed schema validation.');
  const manifestSha256 = sha256(await fs.readFile(manifestPath));
  const attachmentPaths = manifest.files.map((file) => path.join(stagingDir, ...file.relative_staged_path.split('/')));
  return validateContextPackForConsult({
    rootDir: resolvedRoot,
    packetId,
    mode: manifest.mode,
    stagingDir,
    manifestPath,
    manifest,
    manifestSha256,
    packHash: manifest.pack_sha256,
    attachmentPaths,
    attachmentCount: manifest.attachment_count,
    files: manifest.files,
    reusedFiles: manifest.reused_files || [],
  });
}

export async function buildContextPack({
  rootDir,
  packetId = createContextPacketId(),
  mode = CONTEXT_PACK_MODES.NORMAL,
  createdAt = new Date().toISOString(),
  projectGoal = '',
  currentStageGoal = '',
  userVisibleGoal = '',
  establishedFacts = [],
  currentMethod = '',
  currentBlocker = '',
  protectedForbiddenScope = [],
  hardConstraints = [],
  previousRelevantDecisions = [],
  previousRecommendation = undefined,
  latestResult = {},
  metrics = undefined,
  diffSummary = undefined,
  sourceContext = undefined,
  evidence = [],
  previousPack = undefined,
  gitMetadata = undefined,
  evidenceRoots = undefined,
} = {}) {
  if (typeof rootDir !== 'string' || !rootDir.trim()) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'rootDir is required.');
  if (!isValidContextPacketId(packetId)) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'packetId is invalid.');
  const normalizedMode = normalizeMode(mode);
  assertFreshInputContract(arguments[0] || {}, latestResult, previousRelevantDecisions, normalizedMode);
  if (normalizedMode === CONTEXT_PACK_MODES.FRESH && previousPack !== undefined) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'Fresh packets cannot inherit a previous packet.');
  }
  const resolvedRoot = await fs.realpath(path.resolve(rootDir)).catch((error) => {
    throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'rootDir could not be canonicalized.', undefined, error);
  });
  const rawRoots = evidenceRoots === undefined ? [resolvedRoot] : evidenceRoots;
  if (!Array.isArray(rawRoots) || rawRoots.length === 0) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'At least one explicit evidence root is required.');
  const sourceRoots = [];
  for (const root of rawRoots) {
    const realRoot = await fs.realpath(path.resolve(resolvedRoot, root)).catch((error) => {
      throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'An explicit evidence root could not be verified.', undefined, error);
    });
    const stat = await fs.stat(realRoot);
    if (!stat.isDirectory()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'Evidence roots must be directories.');
    if (!sourceRoots.some((item) => path.resolve(item) === path.resolve(realRoot))) sourceRoots.push(realRoot);
  }
  const git = await readGitMetadata(resolvedRoot, gitMetadata);
  const stageContext = Buffer.from(renderStageContext({
    projectGoal,
    currentStageGoal,
    userVisibleGoal,
    establishedFacts,
    currentMethod,
    currentBlocker,
    protectedForbiddenScope,
    hardConstraints,
    previousRelevantDecisions: previousRelevantDecisions.concat(
      normalizedMode === CONTEXT_PACK_MODES.NORMAL && previousRecommendation !== undefined
        ? [{ text: previousRecommendation, kind: 'gpt_recommendation', source: 'gpt' }]
        : [],
    ),
    mode: normalizedMode,
  }), 'utf8');
  const latest = Buffer.from(renderLatestResult(latestResult), 'utf8');
  const generated = [
    fileDescriptor({ logicalName: 'STAGE_CONTEXT.md', stagedPath: 'STAGE_CONTEXT.md', role: 'stage_context', bytes: stageContext }),
    fileDescriptor({ logicalName: 'LATEST_RESULT.md', stagedPath: 'LATEST_RESULT.md', role: 'result', bytes: latest }),
  ];
  if (metrics !== undefined) {
    const metricText = typeof metrics === 'string' ? ensureTextSafe(metrics, 'metrics') : stableJson(metrics);
    assertNoSecrets(metricText, 'METRICS.json');
    assertNoAbsolutePaths(metricText, 'METRICS.json');
    generated.push(fileDescriptor({ logicalName: 'METRICS.json', stagedPath: 'METRICS.json', role: 'metric', bytes: Buffer.from(metricText, 'utf8'), mediaType: 'application/json' }));
  }
  if (diffSummary !== undefined) {
    const diffText = ensureTextSafe(diffSummary, 'DIFF_SUMMARY.md');
    generated.push(fileDescriptor({ logicalName: 'DIFF_SUMMARY.md', stagedPath: 'DIFF_SUMMARY.md', role: 'diff', bytes: Buffer.from(diffText, 'utf8') }));
  }
  const sourceText = renderSourceContext(sourceContext);
  if (sourceText !== null) generated.push(fileDescriptor({ logicalName: 'SOURCE_CONTEXT.md', stagedPath: 'SOURCE_CONTEXT.md', role: 'source', bytes: Buffer.from(sourceText, 'utf8') }));

  const explicit = [];
  for (const descriptor of normalizeEvidenceDescriptors(evidence)) explicit.push(await readExplicitSource(descriptor, { rootDir: resolvedRoot, sourceRoots }));
  const files = deduplicateEvidence(generated.concat(explicit));
  const previousManifest = normalizePreviousManifest(previousPack);
  // A delta packet stores only newly uploaded files in `files`; unchanged
  // files live in `reused_files`. Merge both indexes so a second delta (A→B→C)
  // can continue reusing context inherited from A instead of re-uploading it.
  const previousFiles = new Map((previousManifest?.files || []).map((item) => [item.logical_name, item]));
  for (const item of previousManifest?.reused_files || []) {
    if (!previousFiles.has(item.logical_name)) previousFiles.set(item.logical_name, item);
  }
  const reusedFiles = [];
  const attachedFiles = [];
  for (const file of files) {
    const previous = previousFiles.get(file.logicalName);
    if (
      normalizedMode === CONTEXT_PACK_MODES.NORMAL
      && previous
      && previous.sha256 === file.digest
      && (previous.role === undefined || previous.role === file.role)
    ) {
      reusedFiles.push({
        logical_name: file.logicalName,
        source_packet_id: previousManifest.packet_id,
        relative_staged_path: previous.relative_staged_path,
        sha256: previous.sha256,
        role: file.role,
      });
      continue;
    }
    attachedFiles.push(file);
  }
  if (attachedFiles.length > MAX_CONTEXT_ATTACHMENTS) {
    throw fail(CONTEXT_PACK_FAILURE_CODES.ATTACHMENT_COUNT_EXCEEDED, `A context pack may attach at most ${MAX_CONTEXT_ATTACHMENTS} files.`);
  }
  const normalizedReusedFiles = reusedFiles.sort((left, right) => compareStrings(left.logical_name, right.logical_name));
  const manifestDraft = manifestForPack({
    packetId,
    mode: normalizedMode,
    createdAt: text(createdAt),
    git,
    files: attachedFiles,
    reusedFiles: normalizedReusedFiles,
    packHash: '0'.repeat(64),
  });
  const hash = computeContextPackHash(manifestDraft);
  const manifest = { ...manifestDraft, pack_sha256: hash };
  const manifestText = stableJson(manifest);
  assertNoSecrets(manifestText, CONTEXT_MANIFEST_NAME);
  assertNoAbsolutePaths(manifestText, CONTEXT_MANIFEST_NAME);
  await assertCanonicalStagingBoundary({
    rootDir: resolvedRoot,
    packetId,
    allowMissingPacket: true,
    createParents: true,
  });
  const stagingDir = path.join(resolvedRoot, '.consultations', CONTEXT_PACK_STAGING_DIR, packetId);
  try {
    const existing = await fs.lstat(stagingDir).catch((error) => {
      if (error?.code === 'ENOENT') return null;
      throw error;
    });
    if (existing?.isSymbolicLink()) throw fail(CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION, 'The context packet path cannot be a symlink.');
    if (existing) throw fail(CONTEXT_PACK_FAILURE_CODES.ALREADY_EXISTS, `Context packet ${packetId} already exists.`);
    await fs.mkdir(stagingDir, { recursive: false, mode: 0o700 });
    await assertCanonicalStagingBoundary({ rootDir: resolvedRoot, packetId, stagingDir });
  } catch (error) {
    if (error instanceof ContextPackError) throw error;
    if (error?.code === 'EEXIST') throw fail(CONTEXT_PACK_FAILURE_CODES.ALREADY_EXISTS, `Context packet ${packetId} already exists.`, undefined, error);
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'The context packet staging directory could not be created.', undefined, error);
  }
  await fs.mkdir(path.join(stagingDir, 'evidence'), { recursive: true, mode: 0o700 });
  try {
    for (const file of attachedFiles) {
      const target = path.resolve(stagingDir, ...file.stagedPath.split('/'));
      if (!isPathInside(stagingDir, target)) throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, `Staged path ${file.logicalName} escaped the packet.`);
      await fs.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
      await fs.writeFile(target, file.bytes, { mode: 0o600 });
    }
    const manifestPath = path.join(stagingDir, CONTEXT_MANIFEST_NAME);
    await fs.writeFile(manifestPath, manifestText, { mode: 0o600 });
    const manifestSha256 = sha256(Buffer.from(manifestText, 'utf8'));
    const attachmentPaths = attachedFiles
      .slice()
      .sort((left, right) => compareStrings(left.logicalName, right.logicalName) || compareStrings(left.stagedPath, right.stagedPath))
      .map((file) => path.resolve(stagingDir, ...file.stagedPath.split('/')));
    return {
      packetId,
      mode: normalizedMode,
      rootDir: resolvedRoot,
      stagingDir,
      manifestPath,
      manifest,
      manifestSha256,
      packHash: hash,
      attachmentPaths,
      attachmentCount: attachedFiles.length,
      files: manifest.files,
      reusedFiles: normalizedReusedFiles,
    };
  } catch (error) {
    if (error instanceof ContextPackError) throw error;
    throw fail(CONTEXT_PACK_FAILURE_CODES.INVALID, 'The context packet could not be staged.', undefined, error);
  }
}
