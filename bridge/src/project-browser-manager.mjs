import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { execFile, spawn } from 'node:child_process';
import { chromium } from 'playwright';

// This module owns only machine-local browser resources.  It deliberately
// does not know about Stages, journals, budgets, or Workflow transitions.
export const PROJECT_BROWSER_REGISTRY_VERSION = 'project_browser_registry.v1';
export const DEFAULT_PROJECT_BROWSER_RUNTIME_ROOT = path.resolve(
  process.env.RESEARCH_WORKFLOW_MACHINE_RUNTIME_ROOT
    || path.join(process.env.LOCALAPPDATA || os.tmpdir(), 'ResearchWorkflow'),
);
export const PROJECT_BROWSER_LEASE_STALE_AFTER_MS = 10 * 60 * 1000;
export const PROJECT_BROWSER_START_TIMEOUT_MS = 30_000;
export const PROJECT_BROWSER_STOP_TIMEOUT_MS = 2_000;
export const PROJECT_BROWSER_RESTART_LIMIT = 1;
export const PROJECT_BROWSER_RESTART_HISTORY_LIMIT = 8;
export const PROJECT_BROWSER_PROCESS_IDENTITY_TIMEOUT_MS = 2_000;
export const PROJECT_BROWSER_OWNER_DISCOVERY_TIMEOUT_MS = 5_000;
export const PROJECT_BROWSER_PROCESS_LOST = 'PROJECT_BROWSER_PROCESS_LOST';
export const RECOVERABLE_INFRASTRUCTURE_FAILURE = 'RECOVERABLE_INFRASTRUCTURE_FAILURE';
export const BROWSER_PROXY_ENV = 'RESEARCH_WORKFLOW_BROWSER_PROXY';
export const PROJECT_BROWSER_FAILURE_CLASSES = Object.freeze({
  PROCESS_LOST: PROJECT_BROWSER_PROCESS_LOST,
  RECOVERABLE_INFRASTRUCTURE_FAILURE,
});

const PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/;

export function resolveBrowserProxy(explicitValue = undefined) {
  const value = explicitValue === undefined ? process.env[BROWSER_PROXY_ENV] : explicitValue;
  if (typeof value !== 'string' || !value.trim()) return null;
  return value.trim();
}

export class ProjectBrowserError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = 'ProjectBrowserError';
    this.code = code;
    this.cause = cause;
  }
}

export function normalizeProjectIdentity(value) {
  if (typeof value !== 'string' || !PROJECT_ID_PATTERN.test(value.trim())) {
    throw new ProjectBrowserError('PROJECT_ID_INVALID', 'project_id must be a bounded identity token.');
  }
  return value.trim();
}

export function projectIdentityFromUrl(projectUrl) {
  if (typeof projectUrl !== 'string' || !projectUrl.trim()) return null;
  let parsed;
  try {
    parsed = new URL(projectUrl);
  } catch {
    return null;
  }
  const match = parsed.pathname.match(/^\/g\/(g-p-[A-Za-z0-9][A-Za-z0-9._~-]*)\/project\/?$/);
  return match ? match[1] : null;
}

export function resolveProjectIdentity({ projectId, projectUrl } = {}) {
  const fromUrl = projectIdentityFromUrl(projectUrl);
  const explicit = projectId === undefined || projectId === null
    ? null
    : normalizeProjectIdentity(projectId);
  // A Workflow business identity and a ChatGPT Project slug are distinct
  // authorities.  The explicit Workflow project_id owns the browser; the URL
  // slug is only the bound external target and a compatibility fallback when
  // an older caller did not provide the business identity.
  return explicit || fromUrl;
}

export function sha256Path(value) {
  return crypto.createHash('sha256').update(path.resolve(String(value)), 'utf8').digest('hex');
}

function safeRelativePath(root, target) {
  const relative = path.relative(root, target);
  return relative && !relative.startsWith('..') && !path.isAbsolute(relative)
    ? relative.split(path.sep).join('/')
    : '<external-profile>';
}

function isPathWithin(root, target) {
  const relative = path.relative(path.resolve(root), path.resolve(target));
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

function normalizePathForComparison(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const normalized = path.resolve(value).replace(/[\\/]+$/, '');
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized;
}

function samePath(left, right) {
  const normalizedLeft = normalizePathForComparison(left);
  const normalizedRight = normalizePathForComparison(right);
  return Boolean(normalizedLeft && normalizedRight && normalizedLeft === normalizedRight);
}

function safeIdentityString(value, maxChars = 256) {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const text = String(value);
  if (!text || text.length > maxChars || /[\u0000\r\n]/.test(text)) return null;
  return text;
}

function normalizeProcessIdentity(value, pid = undefined) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'string' || typeof value === 'number') {
    const key = safeIdentityString(value);
    return key ? { key, ...(Number.isInteger(pid) && pid > 0 ? { pid } : {}) } : null;
  }
  if (typeof value !== 'object' || Array.isArray(value)) return null;
  const sourcePid = Number.isInteger(value.pid) && value.pid > 0 ? value.pid : pid;
  const startTime = safeIdentityString(
    value.start_time
      ?? value.startTime
      ?? value.creation_time
      ?? value.creationTime
      ?? value.process_start_time
      ?? value.pid_start_time,
    128,
  );
  const key = safeIdentityString(value.key ?? value.identity ?? startTime);
  if (!key) return null;
  const result = {
    key,
    ...(Number.isInteger(sourcePid) && sourcePid > 0 ? { pid: sourcePid } : {}),
  };
  if (startTime) result.start_time = startTime;
  return result;
}

function sameProcessIdentity(expected, actual, pid = undefined) {
  const expectedIdentity = normalizeProcessIdentity(expected, pid);
  const actualIdentity = normalizeProcessIdentity(actual, pid);
  if (!expectedIdentity || !actualIdentity) return false;
  if (expectedIdentity.pid && actualIdentity.pid && expectedIdentity.pid !== actualIdentity.pid) return false;
  return expectedIdentity.key === actualIdentity.key;
}

function isoTimestamp(now = Date.now()) {
  const value = now instanceof Date ? now : new Date(now);
  return Number.isNaN(value.getTime()) ? new Date().toISOString() : value.toISOString();
}

function sha256Text(value) {
  return crypto.createHash('sha256').update(String(value), 'utf8').digest('hex');
}

export function projectBrowserPaths({ projectId, machineRuntimeRoot = DEFAULT_PROJECT_BROWSER_RUNTIME_ROOT } = {}) {
  const normalizedId = normalizeProjectIdentity(projectId);
  const runtimeRoot = path.resolve(machineRuntimeRoot);
  const projectRoot = path.join(runtimeRoot, 'browser-projects', normalizedId);
  return {
    runtimeRoot,
    projectRoot,
    profileDir: path.join(projectRoot, 'profile'),
    registryPath: path.join(projectRoot, 'registry.json'),
    leaseDir: path.join(projectRoot, 'consultation-lease'),
    leaseMetadataPath: path.join(projectRoot, 'consultation-lease', 'lease.json'),
  };
}

export function defaultProjectBrowserProfileDir({ projectId, machineRuntimeRoot } = {}) {
  return projectBrowserPaths({ projectId, machineRuntimeRoot }).profileDir;
}

async function writePrivateJson(filePath, value) {
  const tempPath = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await fs.writeFile(tempPath, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  await fs.rename(tempPath, filePath);
}

async function readJson(filePath) {
  try {
    const value = JSON.parse(await fs.readFile(filePath, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function execFileText(file, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    execFile(
      file,
      args,
      {
        encoding: 'utf8',
        maxBuffer: 4 * 1024 * 1024,
        timeout: timeoutMs,
        windowsHide: true,
      },
      (error, stdout) => {
        if (error) {
          reject(error);
          return;
        }
        resolve(typeof stdout === 'string' ? stdout : '');
      },
    );
  });
}

async function execFileTextWithRetry(file, args, timeoutMs, attempts = 2) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await execFileText(file, args, timeoutMs);
    } catch (error) {
      lastError = error;
      if (attempt + 1 < attempts) {
        await new Promise((resolve) => setTimeout(resolve, 100 * (attempt + 1)));
      }
    }
  }
  throw lastError;
}

function processInfoToIdentity(info) {
  if (!info || !Number.isInteger(info.pid) || info.pid <= 0) return null;
  const identity = normalizeProcessIdentity({
    pid: info.pid,
    start_time: info.start_time ?? info.creation_time,
  }, info.pid);
  return identity;
}

async function readProcProcessInfo(pid) {
  const procRoot = path.join('/proc', String(pid));
  try {
    const stat = await fs.readFile(path.join(procRoot, 'stat'), 'utf8');
    const commandLine = await fs.readFile(path.join(procRoot, 'cmdline'), 'utf8')
      .then((value) => value.replace(/\u0000/g, ' ').trim())
      .catch(() => '');
    const executablePath = await fs.readlink(path.join(procRoot, 'exe')).catch(() => null);
    const commandEnd = stat.lastIndexOf(')');
    if (commandEnd < 0) return null;
    const fields = stat.slice(commandEnd + 1).trim().split(/\s+/);
    // `/proc/<pid>/stat` starts with field 3 after the closing command name;
    // field 22 (process start ticks) is therefore offset 19 here.
    const startTime = fields[19];
    if (!startTime) return null;
    return {
      pid,
      start_time: startTime,
      executable_path: executablePath,
      command_line: commandLine,
    };
  } catch {
    return null;
  }
}

async function readWindowsProcessInfo(pid) {
  const script = [
    `$item = Get-CimInstance Win32_Process -Filter \"ProcessId = ${pid}\"`,
    'if ($null -ne $item) {',
    '$item | Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress',
    '}',
  ].join('; ');
  try {
    const output = await execFileTextWithRetry(
      'powershell.exe',
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      PROJECT_BROWSER_PROCESS_IDENTITY_TIMEOUT_MS,
    );
    if (!output.trim()) return null;
    const item = JSON.parse(output.trim());
    if (!item || Number(item.ProcessId) !== pid) return null;
    return {
      pid,
      start_time: item.CreationDate,
      executable_path: item.ExecutablePath,
      command_line: item.CommandLine,
    };
  } catch {
    return null;
  }
}

async function readPosixProcessInfo(pid) {
  const output = await execFileText(
    'ps',
    ['-p', String(pid), '-o', 'lstart=,command='],
    PROJECT_BROWSER_PROCESS_IDENTITY_TIMEOUT_MS,
  ).catch(() => '');
  const line = output.trim();
  if (!line) return null;
  const match = line.match(/^(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s*(.*)$/);
  if (!match) return null;
  return {
    pid,
    start_time: match[1],
    executable_path: null,
    command_line: match[2] || '',
  };
}

async function readProcessInfo(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  if (process.platform === 'win32') return readWindowsProcessInfo(pid);
  if (process.platform === 'linux') return readProcProcessInfo(pid);
  return readPosixProcessInfo(pid);
}

async function processIdentity(pid) {
  const info = await readProcessInfo(pid);
  return processInfoToIdentity(info);
}

function parseWindowsProcessList(value) {
  if (!value) return [];
  const parsed = JSON.parse(value);
  return Array.isArray(parsed) ? parsed : [parsed];
}

async function listLocalProcesses() {
  if (process.platform === 'win32') {
    const script = [
      'Get-CimInstance Win32_Process |',
      'Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine |',
      'ConvertTo-Json -Compress',
    ].join(' ');
    const output = await execFileTextWithRetry(
      'powershell.exe',
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
      PROJECT_BROWSER_OWNER_DISCOVERY_TIMEOUT_MS,
    );
    return parseWindowsProcessList(output.trim()).map((item) => ({
      pid: Number(item.ProcessId),
      start_time: item.CreationDate,
      executable_path: item.ExecutablePath,
      command_line: item.CommandLine || '',
    }));
  }
  if (process.platform === 'linux') {
    let entries;
    try {
      entries = await fs.readdir('/proc', { withFileTypes: true });
    } catch (error) {
      throw new ProjectBrowserError('BROWSER_PROFILE_OWNER_DISCOVERY_FAILED', 'Could not enumerate local processes.', error);
    }
    const pids = entries
      .filter((entry) => entry.isDirectory() && /^\d+$/.test(entry.name))
      .map((entry) => Number(entry.name));
    const processes = await Promise.all(pids.map((pid) => readProcProcessInfo(pid)));
    return processes.filter(Boolean);
  }
  const output = await execFileText(
    'ps',
    ['-axo', 'pid=,lstart=,command='],
    PROJECT_BROWSER_OWNER_DISCOVERY_TIMEOUT_MS,
  );
  return output.split(/\r?\n/).flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s*(.*)$/);
    if (!match) return [];
    return [{
      pid: Number(match[1]),
      start_time: match[2],
      executable_path: null,
      command_line: match[3] || '',
    }];
  });
}

function processExited(child) {
  return (child?.exitCode !== null && child?.exitCode !== undefined)
    || (child?.signalCode !== null && child?.signalCode !== undefined);
}

function waitForChildExit(child, timeoutMs) {
  if (!child || processExited(child)) return Promise.resolve(true);
  if (typeof child.once !== 'function') return Promise.resolve(false);
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener?.('exit', onExit);
      child.removeListener?.('close', onClose);
      resolve(value);
    };
    const onExit = () => finish(true);
    const onClose = () => finish(true);
    child.once('exit', onExit);
    child.once('close', onClose);
    timer = setTimeout(() => finish(processExited(child)), timeoutMs);
  });
}

async function waitForCdp(port, timeoutMs = PROJECT_BROWSER_START_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return true;
    } catch {
      // Chromium may need a bounded amount of time to expose CDP.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new ProjectBrowserError('BROWSER_START_TIMEOUT', 'Project Browser CDP did not become ready.');
}

async function findFreePort() {
  const server = await new Promise((resolve, reject) => {
    const candidate = net.createServer();
    candidate.once('error', reject);
    candidate.listen({ host: '127.0.0.1', port: 0 }, () => resolve(candidate));
  });
  const address = server.address();
  await new Promise((resolve) => server.close(() => resolve()));
  if (!address || typeof address !== 'object' || !Number.isInteger(address.port)) {
    throw new ProjectBrowserError('BROWSER_PORT_UNAVAILABLE', 'Could not allocate a local Chromium CDP port.');
  }
  return address.port;
}

function sanitizedProcessIdentity(value, pid = undefined) {
  const identity = normalizeProcessIdentity(value, pid);
  if (!identity) return null;
  return {
    pid: identity.pid || (Number.isInteger(pid) && pid > 0 ? pid : null),
    identity_digest: sha256Text(identity.key),
    start_time: identity.start_time || null,
  };
}

function sanitizedEvidence(record, { reused = false } = {}) {
  if (!record || typeof record !== 'object') return null;
  const restartHistory = Array.isArray(record.restart_history)
    ? record.restart_history.slice(-PROJECT_BROWSER_RESTART_HISTORY_LIMIT).map((item) => ({
      timestamp: typeof item?.timestamp === 'string' ? item.timestamp : null,
      classification: typeof item?.classification === 'string' ? item.classification : null,
      recovery_classification: typeof item?.recovery_classification === 'string'
        ? item.recovery_classification
        : null,
      reason_code: typeof item?.reason_code === 'string' ? item.reason_code : null,
      previous_process_id: Number.isInteger(item?.previous_process_id) && item.previous_process_id > 0
        ? item.previous_process_id
        : null,
      recovered: item?.recovered === true,
      recovered_at: typeof item?.recovered_at === 'string' ? item.recovered_at : null,
      new_process_id: Number.isInteger(item?.new_process_id) && item.new_process_id > 0
        ? item.new_process_id
        : null,
    })).filter((item) => item.timestamp || item.classification || item.reason_code)
    : [];
  return {
    project_id: record.project_id,
    runtime_instance_id: record.runtime_instance_id,
    process_id: record.process_id,
    process_identity: sanitizedProcessIdentity(
      record.process_identity ?? record.process_start_time,
      record.process_id,
    ),
    profile_path: record.profile_path,
    profile_path_digest: record.profile_path_digest,
    start_timestamp: record.start_timestamp,
    restart_count: record.restart_count,
    consultation_count: record.consultation_count,
    failure_classification: record.failure_classification || null,
    recovery_classification: record.recovery_classification || null,
    restart_history: restartHistory,
    reused,
  };
}

async function readRegistry(paths) {
  const record = await readJson(paths.registryPath);
  if (!record) return null;
  if (record.schema_version !== PROJECT_BROWSER_REGISTRY_VERSION) return null;
  if (record.project_id !== path.basename(paths.projectRoot)) return null;
  if (!Number.isInteger(record.process_id) || !Number.isInteger(record.cdp_port)) return null;
  return { ...record, restart_history: restartHistory(record) };
}

function commandLineText(candidate) {
  if (Array.isArray(candidate?.command_line)) return candidate.command_line.join(' ');
  if (Array.isArray(candidate?.cmdline)) return candidate.cmdline.join(' ');
  if (Array.isArray(candidate?.args)) return candidate.args.join(' ');
  return typeof candidate?.command_line === 'string'
    ? candidate.command_line
    : typeof candidate?.cmdline === 'string'
      ? candidate.cmdline
      : '';
}

function extractSwitchValue(commandLine, switchName) {
  if (typeof commandLine !== 'string' || !commandLine) return null;
  const escaped = switchName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = commandLine.match(new RegExp(`(?:^|\\s)${escaped}(?:=|\\s+)(?:"([^"]*)"|'([^']*)'|([^\\s]+))`, 'i'));
  return match ? (match[1] ?? match[2] ?? match[3] ?? null) : null;
}

function inferProjectIdFromProfile(runtimeRoot, profileDir) {
  if (!runtimeRoot || !profileDir) return null;
  const browserProjectsRoot = path.join(path.resolve(runtimeRoot), 'browser-projects');
  const relative = path.relative(browserProjectsRoot, path.resolve(profileDir));
  const parts = relative.split(path.sep);
  return parts.length === 2 && parts[1] === 'profile' && PROJECT_ID_PATTERN.test(parts[0])
    ? parts[0]
    : null;
}

function normalizeOwnerCandidate(candidate, {
  runtimeRoot,
  profileDir,
  processIdentity: processIdentityValue,
} = {}) {
  const pid = Number(candidate?.process_id ?? candidate?.pid);
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const commandLine = commandLineText(candidate);
  const candidateProfile = candidate?.profile_dir
    ?? candidate?.profileDir
    ?? extractSwitchValue(commandLine, '--user-data-dir');
  const candidateProfileDigest = candidate?.profile_path_digest
    ?? candidate?.profilePathDigest
    ?? extractSwitchValue(commandLine, '--research-workflow-profile-digest');
  const profileMatches = samePath(candidateProfile, profileDir)
    || (typeof candidateProfileDigest === 'string' && candidateProfileDigest.toLowerCase() === sha256Path(profileDir));
  if (!profileMatches) return null;
  const resolvedProfile = candidateProfile ? path.resolve(candidateProfile) : path.resolve(profileDir);
  const projectId = safeIdentityString(
    candidate?.project_id
      ?? candidate?.projectId
      ?? extractSwitchValue(commandLine, '--research-workflow-project-id')
      ?? extractSwitchValue(commandLine, '--project-browser-project-id')
      ?? inferProjectIdFromProfile(runtimeRoot, resolvedProfile),
  128);
  const projectUrl = safeIdentityString(
    candidate?.project_url
      ?? candidate?.projectUrl
      ?? extractSwitchValue(commandLine, '--research-workflow-project-url')
      ?? extractSwitchValue(commandLine, '--project-browser-project-url'),
  512);
  const cdpPortValue = candidate?.cdp_port
    ?? candidate?.cdpPort
    ?? extractSwitchValue(commandLine, '--remote-debugging-port');
  const cdpPort = Number(cdpPortValue);
  const identity = normalizeProcessIdentity(
    candidate?.process_identity ?? processIdentityValue ?? processInfoToIdentity(candidate),
    pid,
  );
  return {
    process_id: pid,
    process_identity: identity,
    profile_dir: resolvedProfile,
    project_id: projectId,
    project_url: projectUrl,
    cdp_port: Number.isInteger(cdpPort) && cdpPort > 0 && cdpPort <= 65_535 ? cdpPort : null,
    start_timestamp: typeof candidate?.start_timestamp === 'string'
      ? candidate.start_timestamp
      : typeof candidate?.start_time === 'string'
        ? candidate.start_time
        : typeof candidate?.creation_time === 'string'
          ? candidate.creation_time
          : null,
    has_remote_debugging_port: Number.isInteger(cdpPort) && cdpPort > 0,
    is_browser_process: !/(?:^|\s)--type(?:=|\s)/i.test(commandLine),
  };
}

/**
 * Find live Chromium owners of a profile from the OS process table.  Registry
 * files are only a secondary hint; this scan is what protects a profile when
 * a bridge process or its registry disappeared while Chromium survived.
 */
export async function discoverProjectBrowserProfileOwners({
  runtimeRoot,
  profileDir,
  listProcessesImpl = listLocalProcesses,
  isProcessAlive = processAlive,
  getProcessIdentity = processIdentity,
} = {}) {
  if (!profileDir) return [];
  let listed;
  try {
    listed = await listProcessesImpl();
  } catch (error) {
    throw new ProjectBrowserError('BROWSER_PROFILE_OWNER_DISCOVERY_FAILED', 'Could not inspect local browser owners.', error);
  }
  if (!Array.isArray(listed)) {
    throw new ProjectBrowserError('BROWSER_PROFILE_OWNER_DISCOVERY_FAILED', 'Local browser owner inspection returned an invalid result.');
  }
  const owners = [];
  for (const candidate of listed) {
    const pid = Number(candidate?.process_id ?? candidate?.pid);
    if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) continue;
    let alive;
    try {
      alive = await isProcessAlive(pid);
    } catch {
      alive = false;
    }
    if (!alive) continue;
    let identity = normalizeProcessIdentity(candidate?.process_identity, pid)
      || processInfoToIdentity(candidate);
    if (!identity) {
      try {
        identity = normalizeProcessIdentity(await getProcessIdentity(pid), pid);
      } catch {
        identity = null;
      }
    }
    const owner = normalizeOwnerCandidate(candidate, {
      runtimeRoot,
      profileDir,
      processIdentity: identity,
    });
    if (owner) owners.push(owner);
  }
  const unique = new Map();
  for (const owner of owners) {
    const existing = unique.get(owner.process_id);
    if (!existing || (!existing.has_remote_debugging_port && owner.has_remote_debugging_port)) {
      unique.set(owner.process_id, owner);
    }
  }
  const values = [...unique.values()];
  const withCdp = values.filter((owner) => owner.has_remote_debugging_port);
  const browserWithCdp = withCdp.filter((owner) => owner.is_browser_process);
  if (browserWithCdp.length > 0) return browserWithCdp;
  return withCdp.length > 0 ? withCdp : values;
}

async function profileIsUsedByAnotherProject({
  runtimeRoot,
  projectId,
  profileDir,
  isAlive = processAlive,
  getProcessIdentity: getIdentity = processIdentity,
}) {
  const browserProjectsRoot = path.join(runtimeRoot, 'browser-projects');
  let entries;
  try {
    entries = await fs.readdir(browserProjectsRoot, { withFileTypes: true });
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name === projectId || !PROJECT_ID_PATTERN.test(entry.name)) continue;
    const candidate = await readJson(path.join(browserProjectsRoot, entry.name, 'registry.json'));
    if (!candidate || candidate.status === 'STOPPED' || !samePath(candidate.profile_dir, profileDir)) continue;
    let alive = false;
    try {
      alive = await isAlive(candidate.process_id);
    } catch {
      alive = false;
    }
    if (!alive) continue;
    // A registry PID is not ownership proof.  Only a matching process
    // identity may be used as the fallback when the OS scan did not expose
    // the process command line.
    let identity = null;
    try {
      identity = await getIdentity(candidate.process_id);
    } catch {
      identity = null;
    }
    if (sameProcessIdentity(
      candidate.process_identity ?? candidate.process_start_time,
      identity,
      candidate.process_id,
    )) return true;
  }
  return false;
}

async function acquireLease(paths, {
  projectId,
  isProcessAlive = processAlive,
  getProcessIdentity = processIdentity,
  ownerIdentity = null,
  now = Date.now(),
}) {
  await fs.mkdir(paths.projectRoot, { recursive: true, mode: 0o700 });
  const token = crypto.randomUUID();
  try {
    await fs.mkdir(paths.leaseDir, { recursive: false, mode: 0o700 });
  } catch (error) {
    if (error?.code !== 'EEXIST') throw error;
    let current;
    try {
      current = await readJson(paths.leaseMetadataPath);
    } catch (readError) {
      throw new ProjectBrowserError('PROJECT_BROWSER_LEASE_UNCERTAIN', 'The Project Browser lease metadata is unreadable.', readError);
    }
    if (!current) {
      throw new ProjectBrowserError('PROJECT_BROWSER_LEASE_UNCERTAIN', 'The Project Browser lease owner cannot be verified.');
    }
    let ownerAlive = false;
    try {
      ownerAlive = await isProcessAlive(current.owner_pid);
    } catch {
      ownerAlive = true;
    }
    if (ownerAlive) {
      const storedIdentity = current.owner_process_identity
        ?? current.owner_identity
        ?? current.owner_process_start_time;
      // A legacy lease with no identity is still treated as live.  Age alone
      // is never permission to take a lease from a live process.
      if (!storedIdentity) {
        throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', `Project Browser lease is held for ${projectId}.`);
      }
      let observedIdentity = null;
      try {
        observedIdentity = await getProcessIdentity(current.owner_pid);
      } catch {
        throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', `Project Browser lease is held for ${projectId}.`);
      }
      if (sameProcessIdentity(storedIdentity, observedIdentity, current.owner_pid)) {
        throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', `Project Browser lease is held for ${projectId}.`);
      }
      // The PID is alive but belongs to a different process: it is a stale
      // lease after PID reuse or a machine restart and can be reconciled.
    }
    await fs.rm(paths.leaseDir, { recursive: true, force: true });
    try {
      await fs.mkdir(paths.leaseDir, { recursive: false, mode: 0o700 });
    } catch (mkdirError) {
      if (mkdirError?.code !== 'EEXIST') throw mkdirError;
      throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', `Project Browser lease is held for ${projectId}.`);
    }
  }
  const acquiredAt = isoTimestamp(now);
  const normalizedOwnerIdentity = normalizeProcessIdentity(ownerIdentity, process.pid);
  await writePrivateJson(paths.leaseMetadataPath, {
    schema_version: 'project_browser_lease.v1',
    project_id: projectId,
    owner_pid: process.pid,
    owner_process_identity: normalizedOwnerIdentity,
    owner_process_start_time: normalizedOwnerIdentity?.start_time || null,
    acquired_at: acquiredAt,
    token,
  });
  return { token };
}

async function releaseLease(paths, token) {
  const current = await readJson(paths.leaseMetadataPath);
  if (current?.token && current.token !== token) return;
  await fs.rm(paths.leaseDir, { recursive: true, force: true });
}

async function disconnectBrowser(browser) {
  if (!browser) return;
  if (typeof browser.disconnect === 'function') {
    try {
      browser.disconnect();
    } catch {
      // The browser may have crashed between the health probe and release.
    }
    return;
  }
  // Older Playwright versions do not expose disconnect. Closing the CDP
  // connection is the least destructive fallback; managed processes are
  // still never signalled by this path.
  try {
    await browser.close();
  } catch {
    // Best effort only.
  }
}

async function stopProcess(record, child, {
  isProcessAlive = processAlive,
  getProcessIdentity = processIdentity,
} = {}) {
  const expectedIdentity = record?.process_identity ?? record?.process_start_time;
  if (child && !processExited(child)) {
    if (expectedIdentity) {
      let childIdentity = null;
      try {
        childIdentity = await getProcessIdentity(child.pid);
      } catch {
        childIdentity = null;
      }
      if (sameProcessIdentity(expectedIdentity, childIdentity, child.pid)) {
        try { child.kill(); } catch { /* bounded best effort */ }
        return waitForChildExit(child, PROJECT_BROWSER_STOP_TIMEOUT_MS);
      }
      // The spawned launcher can be different from the CDP-owning Chromium
      // process.  Do not kill the launcher on an identity mismatch; fall
      // through and verify the recorded target PID independently.
    } else {
      try { child.kill(); } catch { /* bounded best effort */ }
      return waitForChildExit(child, PROJECT_BROWSER_STOP_TIMEOUT_MS);
    }
  }
  let alive = false;
  try {
    alive = await isProcessAlive(record?.process_id);
  } catch {
    return false;
  }
  if (!alive) return true;
  if (!expectedIdentity) return false;
  let observedIdentity = null;
  try {
    observedIdentity = await getProcessIdentity(record.process_id);
  } catch {
    return false;
  }
  if (!sameProcessIdentity(expectedIdentity, observedIdentity, record.process_id)) return false;
  try {
    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/PID', String(record.process_id), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      await waitForChildExit(killer, PROJECT_BROWSER_STOP_TIMEOUT_MS);
    } else {
      process.kill(record.process_id, 'SIGTERM');
    }
  } catch {
    // A crash or an already-exited process is an expected recovery outcome.
  }
  // The OS may report the process for a short period after taskkill/SIGTERM;
  // the manager's bounded post-stop poll is the source of truth.
  return true;
}

function withTimeout(promise, timeoutMs, error) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(error);
    }, timeoutMs);
    Promise.resolve(promise).then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (reason) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(reason);
      },
    );
  });
}

function restartHistory(record) {
  if (!Array.isArray(record?.restart_history)) return [];
  return record.restart_history.slice(-PROJECT_BROWSER_RESTART_HISTORY_LIMIT).flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const timestamp = safeIdentityString(item.timestamp, 64);
    const classification = safeIdentityString(item.classification, 64);
    const recoveryClassification = safeIdentityString(item.recovery_classification, 64);
    const reasonCode = safeIdentityString(item.reason_code, 96);
    if (!timestamp && !classification && !reasonCode) return [];
    return [{
      ...(timestamp ? { timestamp } : {}),
      ...(classification ? { classification } : {}),
      ...(recoveryClassification ? { recovery_classification: recoveryClassification } : {}),
      ...(reasonCode ? { reason_code: reasonCode } : {}),
      previous_process_id: Number.isInteger(item.previous_process_id) && item.previous_process_id > 0
        ? item.previous_process_id
        : null,
      previous_process_identity: normalizeProcessIdentity(
        item.previous_process_identity,
        item.previous_process_id,
      ),
      previous_runtime_instance_id: typeof item.previous_runtime_instance_id === 'string'
        ? item.previous_runtime_instance_id.slice(0, 192)
        : null,
      recovered: item.recovered === true,
      ...(typeof item.recovered_at === 'string' ? { recovered_at: item.recovered_at.slice(0, 64) } : {}),
      new_process_id: Number.isInteger(item.new_process_id) && item.new_process_id > 0
        ? item.new_process_id
        : null,
      ...(typeof item.outcome === 'string' ? { outcome: item.outcome.slice(0, 32) } : {}),
    }];
  });
}

function recordRestartFailure(record, {
  classification,
  reasonCode,
  at,
}) {
  const event = {
    timestamp: at,
    classification,
    recovery_classification: RECOVERABLE_INFRASTRUCTURE_FAILURE,
    reason_code: reasonCode,
    previous_process_id: Number.isInteger(record?.process_id) && record.process_id > 0
      ? record.process_id
      : null,
    previous_process_identity: normalizeProcessIdentity(
      record?.process_identity ?? record?.process_start_time,
      record?.process_id,
    ),
    previous_runtime_instance_id: typeof record?.runtime_instance_id === 'string'
      ? record.runtime_instance_id.slice(0, 192)
      : null,
    recovered: false,
  };
  return {
    ...record,
    failure_classification: classification,
    recovery_classification: RECOVERABLE_INFRASTRUCTURE_FAILURE,
    last_failure_code: reasonCode,
    restart_history: [...restartHistory(record), event].slice(-PROJECT_BROWSER_RESTART_HISTORY_LIMIT),
    status: 'RECOVERING',
    updated_at: at,
  };
}

function recordRestartRecovered(record, { processId, at }) {
  const history = restartHistory(record);
  if (history.length === 0) return { ...record, updated_at: at };
  const last = history.at(-1);
  const recoveredEvent = {
    ...last,
    recovered: true,
    recovered_at: at,
    new_process_id: Number.isInteger(processId) && processId > 0 ? processId : null,
  };
  return {
    ...record,
    restart_history: [...history.slice(0, -1), recoveredEvent],
    updated_at: at,
  };
}

function recordRestartFailed(record, { reasonCode, at }) {
  const history = restartHistory(record);
  if (history.length === 0) return { ...record, status: 'STOPPED', updated_at: at };
  const last = history.at(-1);
  return {
    ...record,
    last_failure_code: reasonCode || record.last_failure_code,
    restart_history: [...history.slice(0, -1), {
      ...last,
      recovered: false,
      outcome: 'failed',
    }],
    status: 'STOPPED',
    updated_at: at,
  };
}

export class ProjectBrowserManager {
  constructor({
    projectId,
    projectUrl,
    profileDir,
    repositoryRoot = undefined,
    machineRuntimeRoot = DEFAULT_PROJECT_BROWSER_RUNTIME_ROOT,
    log = () => {},
    chromiumImpl = chromium,
    spawnImpl = spawn,
    waitForCdpImpl = waitForCdp,
    findFreePortImpl = findFreePort,
    processAliveImpl = processAlive,
    stopProcessImpl = stopProcess,
    processIdentityImpl = undefined,
    getProcessIdentityImpl = undefined,
    listProcessesImpl = listLocalProcesses,
    profileOwnerDiscoveryImpl = undefined,
    discoverProfileOwnersImpl = undefined,
    nowImpl = () => Date.now(),
    browserProxy = undefined,
  } = {}) {
    this.projectId = normalizeProjectIdentity(projectId);
    this.projectUrl = projectUrl;
    this.machineRuntimeRoot = path.resolve(machineRuntimeRoot);
    this.paths = projectBrowserPaths({ projectId: this.projectId, machineRuntimeRoot: this.machineRuntimeRoot });
    this.profileDir = path.resolve(profileDir || this.paths.profileDir);
    if (repositoryRoot && isPathWithin(repositoryRoot, this.profileDir)) {
      throw new ProjectBrowserError('PROFILE_IN_REPOSITORY', 'Project Browser profiles must remain outside the business repository.');
    }
    this.log = log;
    this.chromium = chromiumImpl;
    this.spawn = spawnImpl;
    this.waitForCdp = waitForCdpImpl;
    this.findFreePort = findFreePortImpl;
    this.isProcessAlive = processAliveImpl;
    this.stopProcess = stopProcessImpl;
    this.getProcessIdentity = getProcessIdentityImpl || processIdentityImpl || processIdentity;
    this.listProcesses = listProcessesImpl;
    this.profileOwnerDiscovery = discoverProfileOwnersImpl
      || profileOwnerDiscoveryImpl
      || ((options) => discoverProjectBrowserProfileOwners({
        ...options,
        listProcessesImpl: this.listProcesses,
        getProcessIdentity: this.getProcessIdentity,
      }));
    this.nowImpl = nowImpl;
    this.browserProxy = resolveBrowserProxy(browserProxy);
    this.handle = null;
  }

  #now() {
    return isoTimestamp(this.nowImpl());
  }

  async #alive(pid) {
    try {
      return Boolean(await this.isProcessAlive(pid));
    } catch {
      return false;
    }
  }

  async #identity(pid) {
    return normalizeProcessIdentity(await this.getProcessIdentity(pid), pid);
  }

  async #inspect(record) {
    const alive = await this.#alive(record?.process_id);
    if (!alive) return { alive: false, identity: null, identityAvailable: false, matches: false };
    let identity;
    try {
      identity = await this.#identity(record.process_id);
    } catch (error) {
      return { alive: true, identity: null, identityAvailable: false, matches: false, error };
    }
    if (!identity) return { alive: true, identity: null, identityAvailable: false, matches: false };
    return {
      alive: true,
      identity,
      identityAvailable: true,
      matches: sameProcessIdentity(
        record.process_identity ?? record.process_start_time,
        identity,
        record.process_id,
      ),
    };
  }

  async #discoverProfileOwners(record) {
    const result = await withTimeout(
      this.profileOwnerDiscovery({
        runtimeRoot: this.machineRuntimeRoot,
        projectId: this.projectId,
        projectUrl: this.projectUrl,
        profileDir: this.profileDir,
        record,
        isProcessAlive: this.isProcessAlive,
        getProcessIdentity: this.getProcessIdentity,
        listProcessesImpl: this.listProcesses,
      }),
      PROJECT_BROWSER_OWNER_DISCOVERY_TIMEOUT_MS,
      new ProjectBrowserError('BROWSER_PROFILE_OWNER_DISCOVERY_FAILED', 'Local browser owner inspection timed out.'),
    );
    const candidates = Array.isArray(result)
      ? result
      : Array.isArray(result?.owners)
        ? result.owners
        : result && typeof result === 'object'
          ? [result]
          : [];
    return candidates.map((candidate) => normalizeOwnerCandidate(candidate, {
      runtimeRoot: this.machineRuntimeRoot,
      profileDir: this.profileDir,
      processIdentity: candidate?.process_identity,
    })).filter(Boolean);
  }

  #ownerBinding(owner, record) {
    const ownerProjectId = owner.project_id;
    const projectMatches = ownerProjectId === this.projectId
      || (!ownerProjectId && record?.project_id === this.projectId && owner.process_id === record.process_id);
    if (!projectMatches) return ownerProjectId ? 'other' : 'unknown';
    if (owner.project_url && this.projectUrl && owner.project_url !== this.projectUrl) return 'other';
    if (this.projectUrl && !owner.project_url && record?.project_url !== this.projectUrl) return 'unknown';
    return 'same';
  }

  #selectProfileOwner(owners, record) {
    const same = [];
    for (const owner of owners) {
      const binding = this.#ownerBinding(owner, record);
      if (binding === 'same') {
        same.push(owner);
        continue;
      }
      if (binding === 'other' && owner.project_id === this.projectId && owner.project_url) {
        throw new ProjectBrowserError('PROJECT_BROWSER_URL_MISMATCH', 'The live Project Browser binding does not match the requested URL.');
      }
      throw new ProjectBrowserError('PROFILE_ALREADY_IN_USE', 'The configured browser profile has an unassigned or different live owner.');
    }
    if (same.length > 1) {
      throw new ProjectBrowserError('PROFILE_ALREADY_IN_USE', 'The configured browser profile has multiple live Project Browser owners.');
    }
    return same[0] || null;
  }

  #recordFromProfileOwner(owner, previousRecord) {
    const ownerIdentity = normalizeProcessIdentity(owner.process_identity, owner.process_id);
    const previousIdentityMatches = Boolean(
      previousRecord
      && ownerIdentity
      && sameProcessIdentity(
        previousRecord.process_identity ?? previousRecord.process_start_time,
        ownerIdentity,
        owner.process_id,
      ),
    );
    const cdpPort = owner.cdp_port || (
      previousRecord
      && previousRecord.process_id === owner.process_id
      ? previousRecord.cdp_port
      : null
    );
    if (!ownerIdentity || !Number.isInteger(cdpPort) || cdpPort <= 0) {
      throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_UNAVAILABLE', 'The live profile owner cannot be safely adopted.');
    }
    const at = this.#now();
    return {
      ...(previousRecord || {}),
      schema_version: PROJECT_BROWSER_REGISTRY_VERSION,
      project_id: this.projectId,
      project_url: this.projectUrl || previousRecord?.project_url || owner.project_url || null,
      profile_dir: this.profileDir,
      profile_path: safeRelativePath(this.machineRuntimeRoot, this.profileDir),
      profile_path_digest: sha256Path(this.profileDir),
      process_id: owner.process_id,
      process_identity: ownerIdentity,
      process_start_time: ownerIdentity.start_time || null,
      cdp_port: cdpPort,
      runtime_instance_id: previousIdentityMatches && previousRecord.runtime_instance_id
        ? previousRecord.runtime_instance_id
        : `${this.projectId}-${crypto.randomUUID()}`,
      start_timestamp: previousIdentityMatches
        ? previousRecord.start_timestamp
        : owner.start_timestamp || at,
      restart_count: Number.isInteger(previousRecord?.restart_count) ? previousRecord.restart_count : 0,
      consultation_count: Number.isInteger(previousRecord?.consultation_count)
        ? previousRecord.consultation_count
        : 0,
      status: 'READY',
      current_lease: null,
      updated_at: at,
    };
  }

  async #stopVerifiedProcess(record, child = undefined) {
    const observation = await this.#inspect(record);
    if (!observation.alive) return true;
    if (!observation.matches) return false;
    let result;
    try {
      result = await this.stopProcess(record, child, {
        isProcessAlive: this.isProcessAlive,
        getProcessIdentity: this.getProcessIdentity,
      });
    } catch {
      return false;
    }
    if (result === false) return false;
    const deadline = Date.now() + PROJECT_BROWSER_STOP_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (!(await this.#alive(record.process_id))) return true;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return !(await this.#alive(record.process_id));
  }

  async #prepareRestart(record, error, observation) {
    const classification = observation.alive && observation.matches
      ? RECOVERABLE_INFRASTRUCTURE_FAILURE
      : PROJECT_BROWSER_PROCESS_LOST;
    if (observation.alive && observation.matches) {
      const stopped = await this.#stopVerifiedProcess(record);
      if (!stopped) {
        throw new ProjectBrowserError('BROWSER_STOP_UNSAFE', 'The failed Project Browser could not be stopped with a matching process identity.', error);
      }
    }
    const marked = recordRestartFailure(record, {
      classification,
      reasonCode: typeof error?.code === 'string' ? error.code : 'BROWSER_HEALTH_PROBE_FAILED',
      at: this.#now(),
    });
    await writePrivateJson(this.paths.registryPath, marked);
    return marked;
  }

  async #start(record) {
    await fs.mkdir(this.profileDir, { recursive: true, mode: 0o700 });
    const cdpPort = await this.findFreePort();
    const executable = this.chromium.executablePath();
    const args = [
      `--user-data-dir=${this.profileDir}`,
      '--remote-debugging-address=127.0.0.1',
      `--remote-debugging-port=${cdpPort}`,
      '--no-first-run',
      '--no-default-browser-check',
      `--research-workflow-project-id=${this.projectId}`,
      `--research-workflow-profile-digest=${sha256Path(this.profileDir)}`,
    ];
    if (this.browserProxy) args.push(`--proxy-server=${this.browserProxy}`);
    if (this.projectUrl) args.push(`--research-workflow-project-url=${this.projectUrl}`);
    const browserProcess = this.spawn(executable, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: false,
    });
    if (!Number.isInteger(browserProcess?.pid) || browserProcess.pid <= 0) {
      throw new ProjectBrowserError('BROWSER_START_FAILED', 'The Project Browser did not return a usable process identity.');
    }
    browserProcess.unref?.();
    let connected = null;
    let spawnedIdentity = null;
    let identity = null;
    try {
      await this.waitForCdp(cdpPort);
      connected = await this.chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`);
      const context = connected.contexts()[0];
      if (!context) throw new ProjectBrowserError('BROWSER_CONTEXT_UNAVAILABLE', 'New Project Browser has no context.');
      const page = context.pages()[0] || await context.newPage();
      spawnedIdentity = await this.#identity(browserProcess.pid);
      identity = spawnedIdentity;
      // Chromium can leave the CDP-owning browser process as a child of the
      // launcher returned by spawn(). Prefer the verified process carrying the
      // profile/CDP flags so later shutdown and reconnect target the owner,
      // not a short-lived launcher PID.
      let startedOwners = [];
      try {
        startedOwners = await this.#discoverProfileOwners(record);
      } catch (ownerError) {
        if (!identity) throw ownerError;
      }
      const startedOwner = startedOwners.find((owner) => owner.cdp_port === cdpPort
        && owner.project_id === this.projectId
        && owner.process_identity);
      if (startedOwner) {
        identity = startedOwner.process_identity;
      }
      if (!identity) {
        throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_UNAVAILABLE', 'The new Project Browser process identity could not be captured.');
      }
      const managedProcessId = startedOwner?.process_id || browserProcess.pid;
      const now = this.#now();
      const next = {
        ...(record || {}),
        schema_version: PROJECT_BROWSER_REGISTRY_VERSION,
        project_id: this.projectId,
        project_url: this.projectUrl || record?.project_url || null,
        profile_dir: this.profileDir,
        profile_path: safeRelativePath(this.machineRuntimeRoot, this.profileDir),
        profile_path_digest: sha256Path(this.profileDir),
        process_id: managedProcessId,
        process_identity: identity,
        process_start_time: identity.start_time || null,
        cdp_port: cdpPort,
        runtime_instance_id: `${this.projectId}-${crypto.randomUUID()}`,
        start_timestamp: now,
        restart_count: Number.isInteger(record?.restart_count) ? record.restart_count + 1 : 0,
        consultation_count: Number.isInteger(record?.consultation_count) ? record.consultation_count : 0,
        status: 'READY',
        current_lease: null,
        updated_at: now,
      };
      return {
        browser: connected,
        context,
        page,
        browserProcess,
        spawnedProcessRecord: {
          process_id: browserProcess.pid,
          process_identity: spawnedIdentity,
        },
        record: next,
      };
    } catch (error) {
      await disconnectBrowser(connected);
      try {
        await this.stopProcess({
          process_id: browserProcess.pid,
          process_identity: spawnedIdentity,
        }, browserProcess, {
          isProcessAlive: this.isProcessAlive,
          getProcessIdentity: this.getProcessIdentity,
        });
      } catch {
        // Startup cleanup is bounded best effort; the child handle is owned by
        // this manager, but a failed identity probe must never trigger a PID kill.
      }
      throw error;
    }
  }

  async #connect(record) {
    const before = await this.#inspect(record);
    if (!before.alive) throw new ProjectBrowserError('BROWSER_NOT_ALIVE', 'Registered Project Browser is not alive.');
    if (!before.identityAvailable) {
      throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_UNAVAILABLE', 'Registered Project Browser identity could not be verified.');
    }
    if (!before.matches) {
      throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_MISMATCH', 'Registered Project Browser PID belongs to a different process.');
    }
    await this.waitForCdp(record.cdp_port);
    const browser = await this.chromium.connectOverCDP(`http://127.0.0.1:${record.cdp_port}`);
    try {
      const context = browser.contexts()[0];
      if (!context) {
        throw new ProjectBrowserError('BROWSER_CONTEXT_UNAVAILABLE', 'Registered Project Browser has no browser context.');
      }
      const page = context.pages()[0] || await context.newPage();
      const after = await this.#inspect(record);
      if (!after.matches) {
        throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_MISMATCH', 'Registered Project Browser changed during reconnect.');
      }
      return { browser, context, page };
    } catch (error) {
      await disconnectBrowser(browser);
      throw error;
    }
  }

  async acquire() {
    let ownerIdentity = null;
    try {
      ownerIdentity = await this.#identity(process.pid);
    } catch {
      ownerIdentity = null;
    }
    const lease = await acquireLease(this.paths, {
      projectId: this.projectId,
      isProcessAlive: this.isProcessAlive,
      getProcessIdentity: this.getProcessIdentity,
      ownerIdentity,
      now: this.nowImpl(),
    });
    let browser = null;
    let spawnedProcess = null;
    let spawnedProcessRecord = null;
    let record = null;
    let restartAttempts = 0;
    try {
      record = await readRegistry(this.paths);
      if (record && typeof record.profile_dir === 'string' && !samePath(record.profile_dir, this.profileDir)) {
        throw new ProjectBrowserError('PROJECT_BROWSER_PROFILE_MISMATCH', 'Project Browser profile changed while its registry was retained.');
      }
      if (record?.project_url && this.projectUrl && record.project_url !== this.projectUrl) {
        throw new ProjectBrowserError('PROJECT_BROWSER_URL_MISMATCH', 'Project Browser binding changed while its registry was retained.');
      }

      const owners = await this.#discoverProfileOwners(record);
      if (await profileIsUsedByAnotherProject({
        runtimeRoot: this.machineRuntimeRoot,
        projectId: this.projectId,
        profileDir: this.profileDir,
        isAlive: this.isProcessAlive,
        getProcessIdentity: this.getProcessIdentity,
      })) {
        throw new ProjectBrowserError('PROFILE_ALREADY_IN_USE', 'The configured browser profile belongs to another Project Browser.');
      }

      const owner = this.#selectProfileOwner(owners, record);
      if (owner) record = this.#recordFromProfileOwner(owner, record);
      let reused = false;
      let browserProcess = null;
      let connected;
      if (record) {
        const observation = await this.#inspect(record);
        if (observation.alive && !observation.identityAvailable) {
          throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_UNAVAILABLE', 'Registered Project Browser identity could not be verified.');
        }
        if (observation.matches) {
          try {
            connected = await this.#connect(record);
            browser = connected.browser;
            reused = true;
            this.log(`reusing Project Browser project=${this.projectId} pid=${record.process_id}`);
          } catch (error) {
            this.log(`Project Browser health probe failed; restarting project=${this.projectId}`);
            if (restartAttempts >= PROJECT_BROWSER_RESTART_LIMIT) {
              throw new ProjectBrowserError('BROWSER_RESTART_LIMIT_REACHED', 'The bounded Project Browser restart limit was reached.', error);
            }
            restartAttempts += 1;
            record = await this.#prepareRestart(record, error, await this.#inspect(record));
          }
        } else {
          const reason = observation.alive
            ? new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_MISMATCH', 'Registered Project Browser PID belongs to a different process.')
            : new ProjectBrowserError('BROWSER_NOT_ALIVE', 'Registered Project Browser is not alive.');
          if (restartAttempts >= PROJECT_BROWSER_RESTART_LIMIT) {
            throw new ProjectBrowserError('BROWSER_RESTART_LIMIT_REACHED', 'The bounded Project Browser restart limit was reached.', reason);
          }
          restartAttempts += 1;
          record = await this.#prepareRestart(record, reason, observation);
        }
      }
      if (!connected) {
        const started = await this.#start(record);
        connected = { browser: started.browser, context: started.context, page: started.page };
        browserProcess = started.browserProcess;
        browser = connected.browser;
        spawnedProcess = browserProcess;
        spawnedProcessRecord = started.spawnedProcessRecord;
        record = recordRestartRecovered(started.record, {
          processId: started.record.process_id,
          at: this.#now(),
        });
      }
      record.project_url = this.projectUrl || record.project_url || null;
      record.consultation_count = (Number.isInteger(record.consultation_count) ? record.consultation_count : 0) + 1;
      record.current_lease = {
        owner_pid: process.pid,
        owner_process_identity: ownerIdentity,
        acquired_at: this.#now(),
      };
      record.status = 'READY';
      record.updated_at = this.#now();
      await writePrivateJson(this.paths.registryPath, record);
      const handle = {
        ...connected,
        browserProcess,
        record,
        reused,
        evidence: sanitizedEvidence(record, { reused }),
        leaseToken: lease.token,
        released: false,
      };
      this.handle = handle;
      return handle;
    } catch (error) {
      await disconnectBrowser(browser);
      if (spawnedProcess) {
        try {
          await this.stopProcess(spawnedProcessRecord || record, spawnedProcess, {
            isProcessAlive: this.isProcessAlive,
            getProcessIdentity: this.getProcessIdentity,
          });
        } catch {
          // Cleanup must not turn a bounded acquire failure into a second error.
        }
      }
      if (record?.status === 'RECOVERING') {
        try {
          await writePrivateJson(this.paths.registryPath, recordRestartFailed(record, {
            reasonCode: error?.code,
            at: this.#now(),
          }));
        } catch {
          // The registry is rebuildable; do not mask the original acquire error.
        }
      }
      try {
        await releaseLease(this.paths, lease.token);
      } catch {
        // The lease directory is machine-local recovery state; preserve the
        // original error if cleanup itself races with another owner.
      }
      if (error instanceof ProjectBrowserError) throw error;
      throw new ProjectBrowserError('BROWSER_ACQUIRE_FAILED', 'Could not acquire the Project Browser.', error);
    }
  }

  async release(handle = this.handle) {
    if (!handle || handle.released) return true;
    handle.released = true;
    await disconnectBrowser(handle.browser);
    const record = await readRegistry(this.paths);
    if (record && record.runtime_instance_id === handle.record.runtime_instance_id) {
      record.current_lease = null;
      const observation = await this.#inspect(record);
      record.status = observation.matches ? 'READY' : 'STOPPED';
      record.updated_at = this.#now();
      await writePrivateJson(this.paths.registryPath, record);
    }
    await releaseLease(this.paths, handle.leaseToken);
    if (this.handle === handle) this.handle = null;
    return true;
  }

  async #assertShutdownLeaseSafe() {
    let lease;
    try {
      lease = await readJson(this.paths.leaseMetadataPath);
    } catch (error) {
      throw new ProjectBrowserError('PROJECT_BROWSER_LEASE_UNCERTAIN', 'Project Browser shutdown could not verify the lease.', error);
    }
    if (!lease) {
      try {
        await fs.stat(this.paths.leaseDir);
      } catch (error) {
        if (error?.code === 'ENOENT') return;
        throw error;
      }
      throw new ProjectBrowserError('PROJECT_BROWSER_LEASE_UNCERTAIN', 'Project Browser shutdown found an ownerless lease directory.');
    }
    const ownerPid = Number(lease.owner_pid);
    const ownerAlive = await this.#alive(ownerPid);
    if (!ownerAlive) return;
    if (ownerPid === process.pid) return;
    let ownerIdentity = null;
    try {
      ownerIdentity = await this.#identity(ownerPid);
    } catch {
      throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', 'Project Browser is leased by another live process.');
    }
    const storedIdentity = lease.owner_process_identity
      ?? lease.owner_identity
      ?? lease.owner_process_start_time;
    if (!storedIdentity || !ownerIdentity || sameProcessIdentity(storedIdentity, ownerIdentity, ownerPid)) {
      throw new ProjectBrowserError('PROJECT_BROWSER_BUSY', 'Project Browser is leased by another live process.');
    }
  }

  async shutdown() {
    const record = await readRegistry(this.paths);
    if (!record) return false;
    await this.#assertShutdownLeaseSafe();
    const observation = await this.#inspect(record);
    if (observation.alive && !observation.identityAvailable) {
      throw new ProjectBrowserError('BROWSER_PROCESS_IDENTITY_UNAVAILABLE', 'Project Browser shutdown could not verify the process identity.');
    }
    if (observation.alive && observation.matches) {
      const stopped = await this.#stopVerifiedProcess(record, this.handle?.browserProcess);
      if (!stopped) throw new ProjectBrowserError('BROWSER_STOP_UNSAFE', 'Project Browser shutdown refused an unverified process kill.');
    }
    if (this.handle) await disconnectBrowser(this.handle.browser);
    await fs.rm(this.paths.registryPath, { force: true });
    await fs.rm(this.paths.leaseDir, { recursive: true, force: true });
    this.handle = null;
    return true;
  }
}

export async function shutdownProjectBrowser({ projectId, projectUrl, machineRuntimeRoot } = {}) {
  const resolvedId = resolveProjectIdentity({ projectId, projectUrl });
  if (!resolvedId) throw new ProjectBrowserError('PROJECT_ID_REQUIRED', 'A project identity is required to stop a Project Browser.');
  const manager = new ProjectBrowserManager({
    projectId: resolvedId,
    projectUrl,
    machineRuntimeRoot,
  });
  return manager.shutdown();
}
