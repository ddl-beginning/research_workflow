import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import {
  BRIDGE_ROOT,
  CHATGPT_URL,
  CONVERSATION_ID_PATTERN,
  CONVERSATION_MODES,
  BridgeError,
  consultOnce,
  createConsultationId,
  extractConversationIdFromUrl,
  isValidConsultationId,
  isValidConversationUrl,
  isValidProjectUrl,
  readConsultationReceipt,
  validateAssistantBaselineMetadata,
  validateChatgptTargetMetadata,
} from './bridge.mjs';
import { parseDialogueDecision } from './dialogue-policy.mjs';

export const CONSULTATION_INTENT_SCHEMA_VERSION = 'consultation_intent.v1';
export const INTENT_RECOVERY_DIRECTORY = 'intent-recovery';
export const CONSULTATION_INTENT_KEY_PATTERN = /^[0-9a-f]{64}$/i;
export const CONSULTATION_RECOVERY_FAILURE_CODES = Object.freeze({
  INTENT_INVALID: 'CONSULTATION_INTENT_INVALID',
  INTENT_BUSY: 'CONSULTATION_INTENT_BUSY',
  INTENT_CONFLICT: 'CONSULTATION_INTENT_CONFLICT',
  PROMPT_MISMATCH: 'CONSULTATION_PROMPT_MISMATCH',
  RECOVERY_UNRESOLVED: 'CONSULTATION_RECOVERY_UNRESOLVED',
  RESPONSE_INVALID: 'CONSULTATION_RECOVERY_RESPONSE_INVALID',
  LEGACY_INVALID: 'CONSULTATION_RECOVERY_LEGACY_INVALID',
});
export const RECOVERY_FAILURE_CODES = CONSULTATION_RECOVERY_FAILURE_CODES;

const MAX_INTENT_STATUS_CHARS = 96;
const MAX_FAILURE_CODE_CHARS = 96;
const activeIntentLocks = new Map();

function fail(code, message, cause = undefined) {
  return new BridgeError(code, message, cause);
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function sha256Hex(value) {
  return crypto.createHash('sha256').update(String(value), 'utf8').digest('hex');
}

/**
 * Hash the stable reviewer identity. packet ids and generated timestamps are
 * intentionally excluded because consult-pack may rebuild a packet on resume.
 */
export function computeRecoveryPromptHash({ question, packSha256, pack_sha256 } = {}) {
  const packHash = packSha256 === undefined ? pack_sha256 : packSha256;
  if (typeof question !== 'string' || !question.trim()) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.PROMPT_MISMATCH, 'A stable reviewer question is required for recovery.');
  }
  if (typeof packHash !== 'string' || !/^[0-9a-f]{64}$/i.test(packHash)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.PROMPT_MISMATCH, 'A stable context pack hash is required for recovery.');
  }
  return sha256Hex(canonicalJson({
    pack_sha256: packHash.toLowerCase(),
    question: question.trim(),
  }));
}

export const recoveryPromptHash = computeRecoveryPromptHash;

export function isValidConsultationIntentKey(value) {
  return typeof value === 'string' && CONSULTATION_INTENT_KEY_PATTERN.test(value);
}

function assertIntentKey(intentKey) {
  if (!isValidConsultationIntentKey(intentKey)) {
    throw fail(
      CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID,
      'consultation_intent_key must be a 64-character SHA-256 hex digest.',
    );
  }
  return intentKey.toLowerCase();
}

function safeHash(value, fieldName) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/i.test(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, `${fieldName} must be a SHA-256 hex digest.`);
  }
  return value.toLowerCase();
}

function safeProjectUrl(value) {
  if (value === undefined || value === null) return undefined;
  if (!isValidProjectUrl(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable Project URL is invalid.');
  }
  return value;
}

function safeRoute(value, expectedConversationId = undefined) {
  if (value === undefined || value === null) return undefined;
  const conversationId = extractConversationIdFromUrl(value);
  if (!conversationId || (expectedConversationId !== undefined && conversationId !== expectedConversationId)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'The durable conversation route is invalid.');
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'The durable conversation route is invalid.', error);
  }
  return `${parsed.origin}${parsed.pathname}`;
}

function safeMode(value) {
  if (value === undefined) return undefined;
  if (!Object.values(CONVERSATION_MODES).includes(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation mode is invalid.');
  }
  return value;
}

function safeStatus(value) {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !value || value.length > MAX_INTENT_STATUS_CHARS || !/^[A-Za-z0-9._:-]+$/.test(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation status is invalid.');
  }
  return value;
}

function safeFailureCode(value) {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !value || value.length > MAX_FAILURE_CODE_CHARS || !/^[A-Z0-9_:-]+$/.test(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable failure code is invalid.');
  }
  return value;
}

function safePacketId(value) {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !/^PACK-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable packet id is invalid.');
  }
  return value;
}

function validateTargetMetadataSnapshot(value, projectUrl = undefined) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const fields = [
    'chatgpt_target_mode',
    'chatgpt_target_url_digest',
    'chatgpt_target_origin',
    'chatgpt_project_target_verified',
    'fresh_project_chat_created',
  ];
  if (!fields.every((field) => Object.prototype.hasOwnProperty.call(value, field))) return false;
  if (!['PROJECT', 'DEFAULT'].includes(value.chatgpt_target_mode)) return false;
  if (value.chatgpt_target_origin !== 'https://chatgpt.com') return false;
  if (!['YES', 'NO'].includes(value.chatgpt_project_target_verified)) return false;
  if (!['YES', 'NO'].includes(value.fresh_project_chat_created)) return false;
  if (value.chatgpt_target_mode === 'PROJECT') {
    if (typeof projectUrl !== 'string' || !isValidProjectUrl(projectUrl)) return false;
    if (typeof value.chatgpt_target_url_digest !== 'string' || !/^[0-9a-f]{64}$/i.test(value.chatgpt_target_url_digest)) return false;
    if (sha256Hex(projectUrl) !== value.chatgpt_target_url_digest.toLowerCase()) return false;
    return value.chatgpt_project_target_verified === 'YES';
  }
  return value.chatgpt_target_url_digest === null
    && value.chatgpt_project_target_verified === 'NO'
    && value.fresh_project_chat_created === 'NO';
}

function safeTargetMetadata(value, projectUrl = undefined) {
  if (value === undefined) return undefined;
  const source = value && typeof value === 'object' && value.receipt && typeof value.receipt === 'object'
    ? value.receipt
    : value;
  const candidate = source?.chatgpt_target_mode === 'PROJECT'
    && source.project_url === undefined
    && projectUrl
    ? { ...source, project_url: projectUrl }
    : source;
  if (!validateChatgptTargetMetadata(candidate) && !validateTargetMetadataSnapshot(candidate, projectUrl)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable ChatGPT target metadata is invalid.');
  }
  const fields = [
    'chatgpt_target_mode',
    'chatgpt_target_url_digest',
    'chatgpt_target_origin',
    'chatgpt_project_target_verified',
    'fresh_project_chat_created',
  ];
  if (!fields.some((field) => Object.hasOwn(value, field))) return undefined;
  return Object.fromEntries(fields.map((field) => [field, candidate[field]]));
}

function safeBaseline(value) {
  if (value === undefined) return undefined;
  if (!validateAssistantBaselineMetadata(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable assistant baseline metadata is invalid.');
  }
  return {
    schema_version: value.schema_version,
    assistant_count: value.assistant_count,
    id_hashes: [...value.id_hashes],
    slot_hashes: [...value.slot_hashes],
    text_hashes: [...value.text_hashes],
    text_lengths: [...value.text_lengths],
  };
}

function safeConsultationId(value) {
  if (!isValidConsultationId(value)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation id is invalid.');
  }
  return value;
}

function normalizeIntentRecord(record, intentKey, { allowMissingPromptHash = false } = {}) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation intent is not an object.');
  }
  const key = assertIntentKey(intentKey || record.intent_key);
  if (record.intent_key !== undefined && assertIntentKey(record.intent_key) !== key) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_CONFLICT, 'The durable intent key does not match its path.');
  }
  const consultationId = safeConsultationId(record.consultation_id);
  if (!Number.isInteger(record.request_count) || record.request_count < 0 || record.request_count > 1) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable request_count must be 0 or 1.');
  }
  const promptHash = record.prompt_sha256 ?? record.prompt_hash;
  if (promptHash === undefined && !allowMissingPromptHash) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable prompt hash is missing.');
  }
  const normalized = {
    schema_version: CONSULTATION_INTENT_SCHEMA_VERSION,
    intent_key: key,
    consultation_id: consultationId,
    request_count: record.request_count,
  };
  if (promptHash !== undefined) normalized.prompt_sha256 = safeHash(promptHash, 'prompt_sha256');
  if (typeof record.created_at === 'string' && record.created_at) normalized.created_at = record.created_at;
  else normalized.created_at = new Date().toISOString();
  if (typeof record.updated_at === 'string' && record.updated_at) normalized.updated_at = record.updated_at;
  else normalized.updated_at = normalized.created_at;
  for (const [source, target] of [
    ['mode', 'mode'],
    ['status', 'status'],
    ['project_id', 'project_id'],
  ]) {
    if (record[source] !== undefined) {
      if (source === 'project_id' && (typeof record[source] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(record[source]))) {
        throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable project id is invalid.');
      }
      normalized[target] = source === 'mode' ? safeMode(record[source]) : source === 'status' ? safeStatus(record[source]) : record[source];
    }
  }
  const conversationId = record.conversation_id === undefined || record.conversation_id === null
    ? undefined
    : String(record.conversation_id);
  if (conversationId !== undefined && !CONVERSATION_ID_PATTERN.test(conversationId)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'The durable conversation identity is invalid.');
  }
  const route = safeRoute(record.chat_url, conversationId);
  if (route && !conversationId) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'A durable route is missing its conversation identity.');
  }
  if (route) normalized.chat_url = route;
  if (conversationId) normalized.conversation_id = conversationId;
  if (record.conversation_validated !== undefined) {
    if (typeof record.conversation_validated !== 'boolean') throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable conversation validation marker is invalid.');
    normalized.conversation_validated = record.conversation_validated;
  }
  if (record.conversation_root_consultation_id !== undefined && record.conversation_root_consultation_id !== null) {
    normalized.conversation_root_consultation_id = safeConsultationId(record.conversation_root_consultation_id);
  }
  if (record.parent_consultation_id !== undefined && record.parent_consultation_id !== null) {
    normalized.parent_consultation_id = safeConsultationId(record.parent_consultation_id);
  }
  const projectUrl = safeProjectUrl(record.project_url);
  if (projectUrl) normalized.project_url = projectUrl;
  const packetId = safePacketId(record.packet_id);
  if (packetId) normalized.packet_id = packetId;
  const packHash = record.pack_sha256 ?? record.pack_hash;
  if (packHash !== undefined) normalized.pack_sha256 = safeHash(packHash, 'pack_sha256');
  const baseline = safeBaseline(record.baseline_metadata ?? record.assistant_baseline);
  if (baseline) normalized.baseline_metadata = baseline;
  const target = safeTargetMetadata(record.target_metadata || record, record.project_url);
  if (target) normalized.target_metadata = target;
  const failureCode = safeFailureCode(record.failure_code);
  if (failureCode) normalized.failure_code = failureCode;
  if (record.migration_evidence !== undefined) {
    if (!record.migration_evidence || typeof record.migration_evidence !== 'object' || Array.isArray(record.migration_evidence)) {
      throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The migration evidence is invalid.');
    }
    normalized.migration_evidence = { explicit: record.migration_evidence.explicit === true };
  }
  return normalized;
}

export function validateConsultationIntentRecord(record, intentKey = undefined) {
  try {
    normalizeIntentRecord(record, intentKey || record?.intent_key);
    return true;
  } catch {
    return false;
  }
}

function intentPathFor(rootDir, intentKey) {
  const key = assertIntentKey(intentKey);
  return path.resolve(rootDir || BRIDGE_ROOT, '.consultations', INTENT_RECOVERY_DIRECTORY, `${key}.json`);
}

export function consultationIntentPath({ rootDir = BRIDGE_ROOT, intentKey } = {}) {
  return intentPathFor(rootDir, intentKey);
}

async function ensureIntentDirectory(rootDir) {
  const consultationsRoot = path.resolve(rootDir || BRIDGE_ROOT, '.consultations');
  const intentRoot = path.join(consultationsRoot, INTENT_RECOVERY_DIRECTORY);
  await fs.mkdir(intentRoot, { recursive: true, mode: 0o700 });
  return intentRoot;
}

async function atomicWriteJson(filePath, value) {
  const temporaryPath = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporaryPath, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
      flag: 'wx',
    });
    await fs.rename(temporaryPath, filePath);
  } finally {
    await fs.rm(temporaryPath, { force: true }).catch(() => {});
  }
}

export async function readConsultationIntent({ rootDir = BRIDGE_ROOT, intentKey } = {}) {
  const normalizedKey = assertIntentKey(intentKey);
  const filePath = intentPathFor(rootDir, normalizedKey);
  let raw;
  try {
    raw = await fs.readFile(filePath, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation intent could not be read.', error);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_INVALID, 'The durable consultation intent is not valid JSON.', error);
  }
  const record = normalizeIntentRecord(parsed, normalizedKey);
  return { ...record, intent_path: filePath };
}

function mergeIntentRecord(previous, patch, intentKey) {
  if (!previous) return normalizeIntentRecord({ ...patch, intent_key: intentKey }, intentKey);
  const merged = { ...previous, ...patch, intent_key: intentKey };
  if (previous.request_count === 1) merged.request_count = 1;
  if (previous.chat_url && !patch.chat_url) merged.chat_url = previous.chat_url;
  if (previous.conversation_id && !patch.conversation_id) merged.conversation_id = previous.conversation_id;
  if (previous.conversation_validated === true && patch.conversation_validated !== false) merged.conversation_validated = true;
  if (previous.baseline_metadata && !patch.baseline_metadata) merged.baseline_metadata = previous.baseline_metadata;
  if (previous.target_metadata && !patch.target_metadata) merged.target_metadata = previous.target_metadata;
  return normalizeIntentRecord(merged, intentKey);
}

export async function writeConsultationIntent({ rootDir = BRIDGE_ROOT, intentKey, record } = {}) {
  const normalizedKey = assertIntentKey(intentKey || record?.intent_key);
  const intentRoot = await ensureIntentDirectory(rootDir);
  const filePath = intentPathFor(rootDir, normalizedKey);
  const normalized = normalizeIntentRecord(record, normalizedKey);
  await atomicWriteJson(filePath, normalized);
  return { ...normalized, intent_path: path.join(intentRoot, `${normalizedKey}.json`) };
}

export async function updateConsultationIntent({ rootDir = BRIDGE_ROOT, intentKey, patch } = {}) {
  const normalizedKey = assertIntentKey(intentKey);
  const current = await readConsultationIntent({ rootDir, intentKey: normalizedKey });
  const next = mergeIntentRecord(current, {
    ...(patch || {}),
    updated_at: new Date().toISOString(),
  }, normalizedKey);
  return writeConsultationIntent({ rootDir, intentKey: normalizedKey, record: next });
}

async function pidIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

async function acquireIntentLock(rootDir, intentKey) {
  const normalizedKey = assertIntentKey(intentKey);
  const lockPath = path.join(await ensureIntentDirectory(rootDir), `${normalizedKey}.lock`);
  const localKey = path.resolve(lockPath);
  if (activeIntentLocks.has(localKey)) {
    throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_BUSY, 'The consultation intent is already running.');
  }
  const token = crypto.randomUUID();
  const lockRecord = { pid: process.pid, token, created_at: new Date().toISOString() };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await fs.writeFile(lockPath, `${JSON.stringify(lockRecord)}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      activeIntentLocks.set(localKey, token);
      return { lockPath, localKey, token };
    } catch (error) {
      if (error?.code !== 'EEXIST') throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_BUSY, 'The consultation intent lock could not be acquired.', error);
      let existing;
      try {
        existing = JSON.parse(await fs.readFile(lockPath, 'utf8'));
      } catch {
        throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_BUSY, 'The consultation intent has an unreadable live lock.');
      }
      if (await pidIsAlive(existing?.pid)) {
        throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_BUSY, 'The consultation intent is already running.');
      }
      await fs.rm(lockPath, { force: true });
    }
  }
  throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_BUSY, 'The consultation intent lock could not be acquired.');
}

async function releaseIntentLock(lock) {
  if (!lock) return;
  if (activeIntentLocks.get(lock.localKey) === lock.token) activeIntentLocks.delete(lock.localKey);
  let current;
  try {
    current = JSON.parse(await fs.readFile(lock.lockPath, 'utf8'));
  } catch {
    return;
  }
  if (current?.token === lock.token) await fs.rm(lock.lockPath, { force: true });
}

export async function withConsultationIntentLock({ rootDir = BRIDGE_ROOT, intentKey, task } = {}) {
  if (typeof task !== 'function') throw new TypeError('task must be a function');
  const lock = await acquireIntentLock(rootDir, intentKey);
  try {
    return await task();
  } finally {
    await releaseIntentLock(lock);
  }
}

export const withIntentLock = withConsultationIntentLock;

function optionDurabilityHooks(options) {
  const source = options?.durability && typeof options.durability === 'object' ? options.durability : {};
  return {
    beforePromptSend: options?.onBeforePromptSend || source.beforePromptSend || source.beforeSend || source.onBeforePromptSend,
    promptSent: options?.onPromptSent || source.promptSent || source.afterSend || source.onPromptSent,
    conversationRoute: options?.onConversationRoute || source.conversationRoute || source.onConversationRoute || source.route,
  };
}

async function callOptionalHook(hook, payload) {
  if (typeof hook === 'function') await hook(payload);
}

function contextPackMetadata(options) {
  const pack = options?.contextPack;
  if (!pack || typeof pack !== 'object') return {};
  const packetId = pack.packetId || pack.packet_id;
  const packHash = pack.packSha256 || pack.packHash || pack.pack_sha256 || pack.manifest?.pack_sha256;
  const result = {};
  if (packetId !== undefined) result.packet_id = safePacketId(packetId);
  if (packHash !== undefined) result.pack_sha256 = safeHash(packHash, 'pack_sha256');
  return result;
}

function resultRoutePatch(result) {
  const conversationId = result?.conversationId || extractConversationIdFromUrl(result?.chatUrl);
  const chatUrl = result?.chatUrl || (conversationId ? `https://chatgpt.com/c/${conversationId}` : undefined);
  if (!conversationId || !chatUrl || !isValidConversationUrl(chatUrl, conversationId)) return {};
  return {
    chat_url: safeRoute(chatUrl, conversationId),
    conversation_id: conversationId,
    conversation_validated: result?.conversationValidated !== false,
  };
}

function stablePromptHash(prompt, options) {
  const supplied = options?.recoveryPromptHash || options?.promptSha256 || options?.prompt_hash;
  if (supplied !== undefined) return safeHash(supplied, 'prompt_sha256');
  return sha256Hex(prompt);
}

function buildInitialIntent({ rootDir, intentKey, consultationId, promptHash, options, receipt = undefined, recoverConsultationId = undefined }) {
  const receiptTarget = receipt || {};
  const context = contextPackMetadata(options);
  const requestCount = Number.isInteger(receiptTarget.request_count) ? receiptTarget.request_count : 0;
  const route = receiptTarget.chat_url && receiptTarget.conversation_id
    ? safeRoute(receiptTarget.chat_url, receiptTarget.conversation_id)
    : undefined;
  return normalizeIntentRecord({
    schema_version: CONSULTATION_INTENT_SCHEMA_VERSION,
    intent_key: intentKey,
    consultation_id: consultationId,
    request_count: requestCount,
    prompt_sha256: receiptTarget.prompt_sha256 || promptHash,
    created_at: receiptTarget.created_at || new Date().toISOString(),
    updated_at: new Date().toISOString(),
    mode: receiptTarget.mode || options.mode || CONVERSATION_MODES.FRESH,
    status: receiptTarget.status || (recoverConsultationId ? 'legacy_adopted' : 'started'),
    ...(options.projectId === undefined ? {} : { project_id: options.projectId }),
    ...(options.projectUrl === undefined && !receiptTarget.project_url ? {} : { project_url: options.projectUrl || receiptTarget.project_url }),
    ...(context.packet_id ? { packet_id: context.packet_id } : {}),
    ...(context.pack_sha256 ? { pack_sha256: context.pack_sha256 } : {}),
    ...(route ? { chat_url: route, conversation_id: receiptTarget.conversation_id, conversation_validated: receiptTarget.conversation_validated === true } : {}),
    ...(receiptTarget.conversation_root_consultation_id ? { conversation_root_consultation_id: receiptTarget.conversation_root_consultation_id } : {}),
    ...(receiptTarget.parent_consultation_id ? { parent_consultation_id: receiptTarget.parent_consultation_id } : {}),
    ...(receiptTarget.assistant_baseline ? { baseline_metadata: receiptTarget.assistant_baseline } : {}),
    ...(safeTargetMetadata(receiptTarget, receiptTarget.project_url) ? { target_metadata: safeTargetMetadata(receiptTarget, receiptTarget.project_url) } : {}),
    ...(recoverConsultationId ? { migration_evidence: { explicit: true } } : {}),
  }, intentKey);
}

function attachRecoveryFailure(error, intent, rootDir) {
  const failure = error instanceof BridgeError
    ? error
    : fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'The consultation response could not be recovered.', error);
  failure.consultationId = intent?.consultation_id;
  failure.requestCount = Math.max(intent?.request_count || 0, Number.isInteger(error?.requestCount) ? error.requestCount : 0);
  if (!failure.artifacts && intent?.consultation_id) {
    failure.artifacts = {
      consultationDir: path.join(rootDir, '.consultations', intent.consultation_id),
      receiptPath: path.join(rootDir, '.consultations', intent.consultation_id, 'receipt.json'),
    };
  }
  return failure;
}

function promptHashMismatch(expected, actual) {
  return fail(
    CONSULTATION_RECOVERY_FAILURE_CODES.PROMPT_MISMATCH,
    `The recovery prompt identity does not match the durable intent (${expected} != ${actual}).`,
  );
}

/**
 * Execute a canonical consultation intent exactly once. When no intent key is
 * supplied this is a transparent legacy pass-through to consultOnce.
 */
export async function consultWithRecovery(
  prompt,
  options = {},
  { intentKey, recoverConsultationId } = {},
) {
  if (intentKey === undefined && recoverConsultationId === undefined) return consultOnce(prompt, options);
  const normalizedKey = assertIntentKey(intentKey);
  if (recoverConsultationId !== undefined) safeConsultationId(recoverConsultationId);
  const rootDir = path.resolve(options.rootDir || BRIDGE_ROOT);
  // The prompt can be rebuilt from a regenerated packet after a restart. Once
  // the durable intent has crossed the one-request boundary, its stored hash
  // is the authoritative identity for read-only recovery; accepting the
  // rebuilt text here cannot authorize a second send because request_count=1
  // takes the recovery branch below.
  let actualPromptHash = stablePromptHash(prompt, options);
  const runner = typeof options.consult === 'function' ? options.consult : consultOnce;
  const userHooks = optionDurabilityHooks(options);
  const {
    consult: _consult,
    validateRecoveredResponse: _validateRecoveredResponse,
    responseValidator: _responseValidator,
    durability: _durability,
    onBeforePromptSend: _onBeforePromptSend,
    onPromptSent: _onPromptSent,
    onConversationRoute: _onConversationRoute,
    recoveryPromptHash: _recoveryPromptHash,
    prompt_hash: _promptHash,
    ...baseOptions
  } = options || {};

  return withConsultationIntentLock({ rootDir, intentKey: normalizedKey, task: async () => {
    let intent = await readConsultationIntent({ rootDir, intentKey: normalizedKey });
    let receiptRecord = null;
    if (recoverConsultationId !== undefined) {
      if (intent && intent.consultation_id !== recoverConsultationId) {
        throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_CONFLICT, 'The explicit legacy consultation id does not match the durable intent binding.');
      }
      if (!intent) {
        receiptRecord = await readConsultationReceipt({
          rootDir,
          consultationId: recoverConsultationId,
          allowMissing: false,
        });
        if (!receiptRecord) throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.LEGACY_INVALID, 'The explicitly named legacy consultation was not found.');
        intent = buildInitialIntent({
          rootDir,
          intentKey: normalizedKey,
          consultationId: recoverConsultationId,
          promptHash: actualPromptHash,
          options,
          receipt: receiptRecord.receipt,
          recoverConsultationId,
        });
        await writeConsultationIntent({ rootDir, intentKey: normalizedKey, record: intent });
      }
    }
    if (!intent) {
      intent = buildInitialIntent({
        rootDir,
        intentKey: normalizedKey,
        consultationId: createConsultationId(),
        promptHash: actualPromptHash,
        options,
      });
      await writeConsultationIntent({ rootDir, intentKey: normalizedKey, record: intent });
    }

    if (intent.request_count >= 1 && intent.prompt_sha256) {
      actualPromptHash = intent.prompt_sha256;
    }

    if (intent.prompt_sha256 && intent.prompt_sha256 !== actualPromptHash) {
      throw promptHashMismatch(intent.prompt_sha256, actualPromptHash);
    }
    if (!intent.prompt_sha256) {
      intent = await updateConsultationIntent({
        rootDir,
        intentKey: normalizedKey,
        patch: { prompt_sha256: actualPromptHash },
      });
    }

    // The bridge writes its own count=1 receipt immediately before the click.
    // A crash between that write and this intent callback must therefore be
    // promoted from the receipt before we choose the count=0 send path.
    if (!receiptRecord) {
      receiptRecord = await readConsultationReceipt({
        rootDir,
        consultationId: intent.consultation_id,
        allowMissing: true,
      });
    }
    if (receiptRecord?.receipt?.prompt_sha256 && receiptRecord.receipt.prompt_sha256 !== actualPromptHash) {
      throw promptHashMismatch(receiptRecord.receipt.prompt_sha256, actualPromptHash);
    }
    const receiptState = receiptRecord?.receipt;
    const receiptRoute = receiptState
      ? resultRoutePatch({
        chatUrl: receiptState.chat_url,
        conversationId: receiptState.conversation_id,
        conversationValidated: receiptState.conversation_validated,
      })
      : {};
    const receiptTarget = receiptState
      ? safeTargetMetadata(receiptState, receiptState.project_url)
      : undefined;
    const receiptPromotion = receiptState && (
      (receiptState.request_count === 1 && intent.request_count === 0)
      || (!intent.chat_url && receiptRoute.chat_url)
      || (!intent.baseline_metadata && receiptState.assistant_baseline)
      || (!intent.target_metadata && receiptTarget)
    )
      ? {
        ...(receiptState.request_count === 1 ? { request_count: 1, status: 'prompt_sent' } : {}),
        ...receiptRoute,
        ...(receiptState.assistant_baseline ? { baseline_metadata: receiptState.assistant_baseline } : {}),
        ...(receiptTarget ? { target_metadata: receiptTarget } : {}),
      }
      : null;
    if (receiptPromotion) {
      intent = await updateConsultationIntent({
        rootDir,
        intentKey: normalizedKey,
        patch: receiptPromotion,
      });
    }

    const makeDurability = () => ({
      beforePromptSend: async (payload) => {
        intent = await updateConsultationIntent({
          rootDir,
          intentKey: normalizedKey,
          patch: {
            request_count: 1,
            status: 'prompt_pending',
            baseline_metadata: payload?.baselineMetadata,
            ...(payload?.chatUrl && payload?.conversationId ? resultRoutePatch(payload) : {}),
          },
        });
        await callOptionalHook(userHooks.beforePromptSend, payload);
      },
      conversationRoute: async (payload) => {
        const routePatch = resultRoutePatch(payload);
        if (Object.keys(routePatch).length > 0) {
          intent = await updateConsultationIntent({
            rootDir,
            intentKey: normalizedKey,
            patch: { request_count: Math.max(1, intent.request_count), status: 'prompt_sent', ...routePatch },
          });
        }
        await callOptionalHook(userHooks.conversationRoute, payload);
      },
      promptSent: async (payload) => {
        const routePatch = resultRoutePatch(payload);
        intent = await updateConsultationIntent({
          rootDir,
          intentKey: normalizedKey,
          patch: { request_count: 1, status: 'prompt_sent', ...routePatch },
        });
        await callOptionalHook(userHooks.promptSent, payload);
      },
    });

    const commonOptions = {
      ...baseOptions,
      consultationId: intent.consultation_id,
      promptSha256: intent.prompt_sha256,
      consultationIntentKey: normalizedKey,
      preserveReceipt: receiptRecord?.receipt,
      durability: makeDurability(),
    };

    try {
      let result;
      let workflowDecision;
      if (intent.request_count === 0) {
        result = await runner(prompt, commonOptions);
      } else {
        let currentReceipt = receiptRecord;
        if (!currentReceipt) {
          currentReceipt = await readConsultationReceipt({
            rootDir,
            consultationId: intent.consultation_id,
            allowMissing: true,
          });
        }
        const route = intent.chat_url || currentReceipt?.receipt?.chat_url;
        const conversationId = intent.conversation_id || currentReceipt?.receipt?.conversation_id;
        if (!route || !conversationId || !isValidConversationUrl(route, conversationId)) {
          throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RECOVERY_UNRESOLVED, 'The already-counted consultation has no verified conversation route.');
        }
        const receipt = currentReceipt?.receipt;
        const expectedProjectUrl = baseOptions.projectUrl || receipt?.project_url || intent.project_url;
        if (baseOptions.projectUrl && receipt?.project_url && baseOptions.projectUrl !== receipt.project_url) {
          throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.INTENT_CONFLICT, 'The current Project binding does not match the historical receipt.');
        }
        const recovery = {
          chatUrl: route,
          conversationId,
          mode: intent.mode || receipt?.mode || baseOptions.mode || CONVERSATION_MODES.FRESH,
          conversationValidated: true,
          conversationRootConsultationId: intent.conversation_root_consultation_id || receipt?.conversation_root_consultation_id,
          parentConsultationId: intent.parent_consultation_id || receipt?.parent_consultation_id,
          projectScopeVerified: receipt?.project_scope_verified === true,
          projectUrl: expectedProjectUrl,
          baselineMetadata: intent.baseline_metadata || receipt?.assistant_baseline,
          chatgptTargetMetadata: intent.target_metadata || receipt,
          responseValidator: async (responseText) => {
            const validator = options.validateRecoveredResponse || options.responseValidator || parseDialogueDecision;
            let checked;
            try {
              checked = await validator(responseText);
            } catch (error) {
              throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RESPONSE_INVALID, 'The recovered response failed the workflow decision validity check.', error);
            }
            if (!checked) throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RESPONSE_INVALID, 'The recovered response did not contain exactly one valid WORKFLOW_DECISION.');
            workflowDecision = checked;
            return checked;
          },
          responseFailureCode: CONSULTATION_RECOVERY_FAILURE_CODES.RESPONSE_INVALID,
        };
        result = await runner(prompt, {
          ...commonOptions,
          ...(expectedProjectUrl ? { projectUrl: expectedProjectUrl } : {}),
          mode: recovery.mode,
          recovery,
        });
        if (!workflowDecision) {
          const validator = options.validateRecoveredResponse || options.responseValidator || parseDialogueDecision;
          workflowDecision = await validator(result?.responseText);
          if (!workflowDecision) throw fail(CONSULTATION_RECOVERY_FAILURE_CODES.RESPONSE_INVALID, 'The recovered response did not contain exactly one valid WORKFLOW_DECISION.');
        }
      }
      const routePatch = resultRoutePatch(result);
      intent = await updateConsultationIntent({
        rootDir,
        intentKey: normalizedKey,
        patch: {
          request_count: Math.max(intent.request_count, Number.isInteger(result?.requestCount) ? result.requestCount : 0),
          status: 'complete',
          ...routePatch,
        },
      });
      return {
        ...result,
        consultationId: result?.consultationId || intent.consultation_id,
        recovery: { intentKey: normalizedKey, requestCount: intent.request_count },
        ...(workflowDecision ? { workflowDecision } : {}),
      };
    } catch (error) {
      let receiptAfter = null;
      try {
        receiptAfter = await readConsultationReceipt({
          rootDir,
          consultationId: intent.consultation_id,
          allowMissing: true,
        });
      } catch {
        // Preserve the original failure and the last durable intent state.
      }
      const failure = attachRecoveryFailure(error, intent, rootDir);
      const receipt = receiptAfter?.receipt;
      try {
        intent = await updateConsultationIntent({
          rootDir,
          intentKey: normalizedKey,
          patch: {
            request_count: Math.max(intent.request_count, Number.isInteger(failure.requestCount) ? failure.requestCount : 0),
            status: 'failed',
            failure_code: failure.code,
            ...(receipt ? resultRoutePatch({
              chatUrl: receipt.chat_url,
              conversationId: receipt.conversation_id,
              conversationValidated: receipt.conversation_validated,
            }) : {}),
          },
        });
      } catch {
        // The write-ahead record is already conservative. Never turn a
        // substantive recovery failure into a fresh consultation.
      }
      throw failure;
    }
  }});
}
