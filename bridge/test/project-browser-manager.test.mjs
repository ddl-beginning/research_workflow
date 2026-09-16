import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import { spawn as childSpawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { chromium } from 'playwright';
import { ChatGPTBridge } from '../src/bridge.mjs';
import {
  discoverProjectBrowserProfileOwners,
  PROJECT_BROWSER_PROCESS_LOST,
  RECOVERABLE_INFRASTRUCTURE_FAILURE,
  ProjectBrowserError,
  ProjectBrowserManager,
  defaultProjectBrowserProfileDir,
  projectBrowserPaths,
  resolveProjectIdentity,
  sha256Path,
} from '../src/project-browser-manager.mjs';

function fakeChromium() {
  const calls = [];
  const chromium = {
    executablePath() { return 'fake-chromium'; },
    async connectOverCDP(endpoint) {
      calls.push(endpoint);
      const page = {
        setDefaultTimeout() {},
        setDefaultNavigationTimeout() {},
        url() { return 'about:blank'; },
      };
      const context = { pages() { return [page]; }, async newPage() { return page; } };
      const browser = {
        contexts() { return [context]; },
        disconnect() { calls.push('disconnect'); },
      };
      return browser;
    },
  };
  return { chromium, calls };
}

function fakeSpawnFactory() {
  let nextPid = 4000;
  const children = [];
  const alive = new Set();
  const identities = new Map([[process.pid, { key: 'fake-manager', pid: process.pid, start_time: 'manager' }]]);
  const stopCalls = [];
  const spawn = (executable, args) => {
    const child = new EventEmitter();
    child.pid = ++nextPid;
    child.exitCode = null;
    child.signalCode = null;
    child.killed = false;
    child.unref = () => {};
    child.kill = () => {
      child.killed = true;
      alive.delete(child.pid);
      child.exitCode = 0;
      child.emit('exit', 0, null);
      child.emit('close', 0, null);
    };
    alive.add(child.pid);
    identities.set(child.pid, { key: `fake-start-${child.pid}`, pid: child.pid, start_time: `fake-start-${child.pid}` });
    children.push({ child, executable, args });
    return child;
  };
  const isAlive = (pid) => pid === process.pid || alive.has(pid);
  const getIdentity = (pid) => identities.get(pid) || null;
  const stopProcess = async (record, child) => {
    stopCalls.push(record?.process_id ?? child?.pid ?? null);
    const target = child || children.find((entry) => entry.child.pid === record?.process_id)?.child;
    if (target && !target.killed) target.kill();
    if (!target && Number.isInteger(record?.process_id)) alive.delete(record.process_id);
    return true;
  };
  return { spawn, children, alive, identities, isAlive, getIdentity, stopProcess, stopCalls };
}

async function makeManager(root, projectId, projectUrl, options = {}) {
  const chromiumState = options.chromiumState || fakeChromium();
  const { chromium, calls } = chromiumState;
  const spawnState = options.spawnState || fakeSpawnFactory();
  const manager = new ProjectBrowserManager({
    projectId,
    projectUrl,
    machineRuntimeRoot: root,
    chromiumImpl: chromium,
    spawnImpl: spawnState.spawn,
    waitForCdpImpl: async () => {},
    findFreePortImpl: async () => 41_000 + Math.floor(Math.random() * 500),
    processAliveImpl: spawnState.isAlive,
    processIdentityImpl: spawnState.getIdentity,
    listProcessesImpl: options.listProcessesImpl || (async () => []),
    profileOwnerDiscoveryImpl: options.profileOwnerDiscoveryImpl,
    stopProcessImpl: options.stopProcessImpl || spawnState.stopProcess,
  });
  return { manager, calls, spawnState, chromiumState };
}

async function writeJson(filePath, value) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function registryRecord(manager, {
  processId,
  processIdentity,
  cdpPort = 41_001,
  runtimeInstanceId = `${manager.projectId}-old-instance`,
  restartCount = 0,
  restartHistory = [],
  ...extra
} = {}) {
  return {
    schema_version: 'project_browser_registry.v1',
    project_id: manager.projectId,
    project_url: manager.projectUrl || null,
    profile_dir: manager.profileDir,
    profile_path: path.relative(manager.machineRuntimeRoot, manager.profileDir).split(path.sep).join('/'),
    profile_path_digest: manager.profileDir ? sha256Path(manager.profileDir) : null,
    process_id: processId,
    process_identity: processIdentity,
    process_start_time: processIdentity?.start_time || null,
    cdp_port: cdpPort,
    runtime_instance_id: runtimeInstanceId,
    start_timestamp: '2026-09-16T00:00:00.000Z',
    restart_count: restartCount,
    consultation_count: 0,
    restart_history: restartHistory,
    status: 'READY',
    current_lease: null,
    updated_at: '2026-09-16T00:00:00.000Z',
    ...extra,
  };
}

test('Project identity and default profile are canonical and machine-local', async () => {
  assert.equal(resolveProjectIdentity({ projectUrl: 'https://chatgpt.com/g/g-p-alpha/project' }), 'g-p-alpha');
  assert.equal(
    resolveProjectIdentity({ projectId: 'business-alpha', projectUrl: 'https://chatgpt.com/g/g-p-beta/project' }),
    'business-alpha',
  );
  const paths = projectBrowserPaths({ projectId: 'project-A', machineRuntimeRoot: 'C:/runtime' });
  assert.equal(paths.profileDir, path.resolve('C:/runtime/browser-projects/project-A/profile'));
  assert.equal(defaultProjectBrowserProfileDir({ projectId: 'project-A', machineRuntimeRoot: 'C:/runtime' }), paths.profileDir);
});

test('same Project reuses one process/profile after consultation release', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-reuse-'));
  try {
    const url = 'https://chatgpt.com/g/g-p-reuse/project';
    const first = await makeManager(root, 'g-p-reuse', url);
    const firstHandle = await first.manager.acquire();
    await first.manager.release(firstHandle);
    const registry = JSON.parse(await fs.readFile(projectBrowserPaths({ projectId: 'g-p-reuse', machineRuntimeRoot: root }).registryPath, 'utf8'));
    assert.equal(registry.process_id, firstHandle.record.process_id);
    assert.equal(registry.profile_dir, first.manager.profileDir);
    assert.equal(registry.status, 'READY');

    const second = await makeManager(root, 'g-p-reuse', url, { chromiumState: first.chromiumState, spawnState: first.spawnState });
    const secondHandle = await second.manager.acquire();
    assert.equal(secondHandle.reused, true);
    assert.equal(secondHandle.record.process_id, firstHandle.record.process_id);
    assert.equal(secondHandle.record.profile_path_digest, firstHandle.record.profile_path_digest);
    assert.equal(first.spawnState.children.length, 1);
    await second.manager.release(secondHandle);
    assert.equal(first.calls.at(-1), 'disconnect');
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('same Project consultation lease serializes writers', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-lease-'));
  try {
    const one = await makeManager(root, 'g-p-lease', 'https://chatgpt.com/g/g-p-lease/project');
    const handle = await one.manager.acquire();
    const two = await makeManager(root, 'g-p-lease', 'https://chatgpt.com/g/g-p-lease/project');
    await assert.rejects(
      two.manager.acquire(),
      (error) => error instanceof ProjectBrowserError && error.code === 'PROJECT_BROWSER_BUSY',
    );
    await one.manager.release(handle);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a crashed Project Browser restarts only its own process and reuses its profile', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-restart-'));
  try {
    const url = 'https://chatgpt.com/g/g-p-restart/project';
    const first = await makeManager(root, 'g-p-restart', url);
    const firstHandle = await first.manager.acquire();
    await first.manager.release(firstHandle);
    const second = await makeManager(root, 'g-p-restart', url, { chromiumState: first.chromiumState, spawnState: first.spawnState });
    second.manager.isProcessAlive = (pid) => pid !== firstHandle.record.process_id;
    const secondHandle = await second.manager.acquire();
    assert.equal(secondHandle.reused, false);
    assert.notEqual(secondHandle.record.process_id, firstHandle.record.process_id);
    assert.equal(secondHandle.record.profile_path_digest, firstHandle.record.profile_path_digest);
    assert.equal(secondHandle.record.restart_count, 1);
    await second.manager.release(secondHandle);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('different Projects use different processes and profiles', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-isolation-'));
  try {
    const a = await makeManager(root, 'g-p-a', 'https://chatgpt.com/g/g-p-a/project');
    const b = await makeManager(root, 'g-p-b', 'https://chatgpt.com/g/g-p-b/project', { spawnState: a.spawnState });
    const [ha, hb] = await Promise.all([a.manager.acquire(), b.manager.acquire()]);
    assert.notEqual(ha.record.process_id, hb.record.process_id);
    assert.notEqual(a.manager.profileDir, b.manager.profileDir);
    await Promise.all([a.manager.release(ha), b.manager.release(hb)]);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a profile cannot be concurrently assigned to another Project', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-profile-lock-'));
  try {
    const sharedProfile = path.join(root, 'shared-profile');
    const a = await makeManager(root, 'g-p-profile-a', 'https://chatgpt.com/g/g-p-profile-a/project');
    a.manager.profileDir = sharedProfile;
    const ha = await a.manager.acquire();
    const b = await makeManager(root, 'g-p-profile-b', 'https://chatgpt.com/g/g-p-profile-b/project', { spawnState: a.spawnState });
    b.manager.profileDir = sharedProfile;
    await assert.rejects(
      b.manager.acquire(),
      (error) => error instanceof ProjectBrowserError && error.code === 'PROFILE_ALREADY_IN_USE',
    );
    await a.manager.release(ha);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('an existing live profile owner is adopted when its registry is missing', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-owner-discovery-'));
  const spawnState = fakeSpawnFactory();
  const projectId = 'g-p-owner-discovery';
  const projectUrl = 'https://chatgpt.com/g/g-p-owner-discovery/project';
  const profileDir = projectBrowserPaths({ projectId, machineRuntimeRoot: root }).profileDir;
  const ownerPid = 4_501;
  const ownerIdentity = { key: 'owner-start-4501', pid: ownerPid, start_time: 'owner-start-4501' };
  spawnState.alive.add(ownerPid);
  spawnState.identities.set(ownerPid, ownerIdentity);
  const listProcesses = async () => [{
    pid: ownerPid,
    start_time: ownerIdentity.start_time,
    command_line: `chrome.exe --user-data-dir="${profileDir}" --remote-debugging-port=43101 --research-workflow-project-id=${projectId} --research-workflow-project-url=${projectUrl}`,
  }];
  try {
    const owners = await discoverProjectBrowserProfileOwners({
      runtimeRoot: root,
      profileDir,
      listProcessesImpl: listProcesses,
      isProcessAlive: spawnState.isAlive,
      getProcessIdentity: spawnState.getIdentity,
    });
    assert.deepEqual(owners.map((owner) => ({
      process_id: owner.process_id,
      project_id: owner.project_id,
      project_url: owner.project_url,
      cdp_port: owner.cdp_port,
    })), [{
      process_id: ownerPid,
      project_id: projectId,
      project_url: projectUrl,
      cdp_port: 43101,
    }]);

    const managerState = await makeManager(root, projectId, projectUrl, {
      spawnState,
      listProcessesImpl: listProcesses,
    });
    const handle = await managerState.manager.acquire();
    assert.equal(handle.reused, true);
    assert.equal(handle.record.process_id, ownerPid);
    assert.equal(spawnState.children.length, 0);
    await managerState.manager.release(handle);
    await managerState.manager.shutdown();
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('an unregistered live owner for the same profile blocks a different Project', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-owner-conflict-'));
  const spawnState = fakeSpawnFactory();
  const ownerProjectId = 'g-p-owner-a';
  const targetProjectId = 'g-p-owner-b';
  const profileDir = path.join(root, 'shared-profile');
  const ownerPid = 4_502;
  spawnState.alive.add(ownerPid);
  spawnState.identities.set(ownerPid, { key: 'owner-start-4502', pid: ownerPid, start_time: 'owner-start-4502' });
  const listProcesses = async () => [{
    pid: ownerPid,
    start_time: 'owner-start-4502',
    command_line: `chrome.exe --user-data-dir="${profileDir}" --remote-debugging-port=43102 --research-workflow-project-id=${ownerProjectId} --research-workflow-project-url=https://chatgpt.com/g/${ownerProjectId}/project`,
  }];
  try {
    const target = await makeManager(root, targetProjectId, 'https://chatgpt.com/g/g-p-owner-b/project', {
      spawnState,
      listProcessesImpl: listProcesses,
    });
    target.manager.profileDir = profileDir;
    await assert.rejects(
      target.manager.acquire(),
      (error) => error instanceof ProjectBrowserError && error.code === 'PROFILE_ALREADY_IN_USE',
    );
    assert.equal(spawnState.children.length, 0);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a stale PID is never killed and its Project Browser restart records derived recovery evidence', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-stale-pid-'));
  const spawnState = fakeSpawnFactory();
  const managerState = await makeManager(root, 'g-p-stale-pid', 'https://chatgpt.com/g/g-p-stale-pid/project', {
    spawnState,
  });
  const stalePid = 4_503;
  spawnState.alive.add(stalePid);
  spawnState.identities.set(stalePid, { key: 'pid-reused-new-process', pid: stalePid, start_time: 'pid-reused-new-process' });
  await writeJson(managerState.manager.paths.registryPath, registryRecord(managerState.manager, {
    processId: stalePid,
    processIdentity: { key: 'old-browser-process', pid: stalePid, start_time: 'old-browser-process' },
  }));
  try {
    const handle = await managerState.manager.acquire();
    assert.equal(handle.reused, false);
    assert.equal(handle.evidence.failure_classification, PROJECT_BROWSER_PROCESS_LOST);
    assert.equal(handle.evidence.recovery_classification, RECOVERABLE_INFRASTRUCTURE_FAILURE);
    assert.equal(handle.evidence.restart_history.at(-1).classification, PROJECT_BROWSER_PROCESS_LOST);
    assert.equal(handle.evidence.restart_history.at(-1).recovered, true);
    assert.equal(handle.evidence.restart_history.at(-1).previous_process_id, stalePid);
    assert.equal(managerState.spawnState.stopCalls.includes(stalePid), false);
    assert.equal(Object.hasOwn(handle.evidence.process_identity, 'identity_digest'), true);
    assert.equal(Object.hasOwn(handle.evidence.process_identity, 'executable_path'), false);
    assert.doesNotMatch(JSON.stringify(handle.evidence), /command_line|password|cookie|authorization/i);
    await managerState.manager.release(handle);
    await managerState.manager.shutdown();
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a reconnect failure stops only a process with the matching identity before the one bounded restart', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-reconnect-failure-'));
  const spawnState = fakeSpawnFactory();
  const chromiumState = fakeChromium();
  const originalConnect = chromiumState.chromium.connectOverCDP.bind(chromiumState.chromium);
  let connectCount = 0;
  chromiumState.chromium.connectOverCDP = async (endpoint) => {
    connectCount += 1;
    if (connectCount === 2) throw new Error('simulated CDP transport loss');
    return originalConnect(endpoint);
  };
  try {
    const first = await makeManager(root, 'g-p-cdp-loss', 'https://chatgpt.com/g/g-p-cdp-loss/project', {
      spawnState,
      chromiumState,
    });
    const firstHandle = await first.manager.acquire();
    await first.manager.release(firstHandle);
    const second = await makeManager(root, 'g-p-cdp-loss', 'https://chatgpt.com/g/g-p-cdp-loss/project', {
      spawnState,
      chromiumState,
    });
    const secondHandle = await second.manager.acquire();
    assert.equal(secondHandle.reused, false);
    assert.equal(secondHandle.evidence.failure_classification, RECOVERABLE_INFRASTRUCTURE_FAILURE);
    assert.equal(secondHandle.evidence.restart_history.at(-1).classification, RECOVERABLE_INFRASTRUCTURE_FAILURE);
    assert.equal(secondHandle.evidence.restart_history.at(-1).recovered, true);
    assert.equal(spawnState.stopCalls.includes(firstHandle.record.process_id), true);
    await second.manager.release(secondHandle);
    await second.manager.shutdown();
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a live lease remains busy after ten minutes, while a dead owner lease is reconciled', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-lease-reconcile-'));
  const spawnState = fakeSpawnFactory();
  const url = 'https://chatgpt.com/g/g-p-lease-reconcile/project';
  try {
    const first = await makeManager(root, 'g-p-lease-reconcile', url, { spawnState });
    const firstHandle = await first.manager.acquire();
    const leaseMetadata = JSON.parse(await fs.readFile(first.manager.paths.leaseMetadataPath, 'utf8'));
    leaseMetadata.acquired_at = '2000-01-01T00:00:00.000Z';
    await writeJson(first.manager.paths.leaseMetadataPath, leaseMetadata);
    const second = await makeManager(root, 'g-p-lease-reconcile', url, { spawnState });
    await assert.rejects(
      second.manager.acquire(),
      (error) => error instanceof ProjectBrowserError && error.code === 'PROJECT_BROWSER_BUSY',
    );
    await first.manager.release(firstHandle);
    spawnState.alive.delete(firstHandle.record.process_id);

    const stalePaths = projectBrowserPaths({ projectId: 'g-p-lease-reconcile', machineRuntimeRoot: root });
    await fs.mkdir(stalePaths.leaseDir, { recursive: true });
    await writeJson(stalePaths.leaseMetadataPath, {
      schema_version: 'project_browser_lease.v1',
      project_id: 'g-p-lease-reconcile',
      owner_pid: 4_504,
      owner_process_identity: { key: 'machine-before-restart', pid: 4_504, start_time: 'machine-before-restart' },
      acquired_at: '2000-01-01T00:00:00.000Z',
      token: 'stale-owner-token',
    });
    const recovered = await makeManager(root, 'g-p-lease-reconcile', url, { spawnState });
    const recoveredHandle = await recovered.manager.acquire();
    assert.equal(recoveredHandle.reused, false);
    await recovered.manager.release(recoveredHandle);
    await recovered.manager.shutdown();
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('a live PID with a different identity is treated as stale without killing the live process', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-pid-reuse-'));
  const spawnState = fakeSpawnFactory();
  const managerState = await makeManager(root, 'g-p-pid-reuse', 'https://chatgpt.com/g/g-p-pid-reuse/project', { spawnState });
  const pid = 4_505;
  spawnState.alive.add(pid);
  spawnState.identities.set(pid, { key: 'new-owner', pid, start_time: 'new-owner' });
  await fs.mkdir(managerState.manager.paths.leaseDir, { recursive: true });
  await writeJson(managerState.manager.paths.leaseMetadataPath, {
    schema_version: 'project_browser_lease.v1',
    project_id: managerState.manager.projectId,
    owner_pid: pid,
    owner_process_identity: { key: 'old-owner', pid, start_time: 'old-owner' },
    acquired_at: '2000-01-01T00:00:00.000Z',
    token: 'pid-reused-lease',
  });
  try {
    const handle = await managerState.manager.acquire();
    assert.equal(handle.reused, false);
    assert.equal(spawnState.alive.has(pid), true);
    assert.equal(spawnState.stopCalls.includes(pid), false);
    await managerState.manager.release(handle);
    await managerState.manager.shutdown();
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('consultation release does not close a managed Project Browser', async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-release-'));
  const events = [];
  const child = { killed: false };
  const page = {
    setDefaultTimeout() {},
    setDefaultNavigationTimeout() {},
  };
  const handle = {
    browser: { disconnect() { events.push('disconnect'); } },
    context: {},
    page,
    browserProcess: child,
    reused: true,
    evidence: {
      project_id: 'business-release',
      runtime_instance_id: 'business-release-instance',
      process_id: 4567,
      profile_path: 'browser-projects/business-release/profile',
      profile_path_digest: 'a'.repeat(64),
      start_timestamp: new Date().toISOString(),
      restart_count: 0,
      consultation_count: 1,
      reused: true,
    },
  };
  const manager = {
    async acquire() { return handle; },
    async release(value) { assert.equal(value, handle); events.push('release'); },
  };
  try {
    const bridge = new ChatGPTBridge({
      profileDir: path.join(root, 'profile'),
      projectId: 'business-release',
      projectUrl: 'https://chatgpt.com/g/g-p-release/project',
      projectBrowserManagerFactory: () => manager,
    });
    await bridge.open();
    assert.equal(bridge.browserProcessEvidence.project_id, 'business-release');
    assert.equal(await bridge.close(), true);
    assert.deepEqual(events, ['release']);
    assert.equal(child.killed, false);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

const realProjectBrowserTestsEnabled = process.env.PROJECT_BROWSER_REAL_TESTS === '1'
  && existsSync(chromium.executablePath());

function isPidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitForPidExit(pid, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isPidAlive(pid)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return !isPidAlive(pid);
}

async function killRealBrowserProcess(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return;
  if (process.platform === 'win32') {
    await new Promise((resolve) => {
      const killer = childSpawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      killer.once('error', resolve);
      killer.once('close', resolve);
    });
  } else {
    try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
  }
  assert.equal(await waitForPidExit(pid), true);
}

async function removeRealTestRoot(root) {
  await fs.rm(root, {
    recursive: true,
    force: true,
    maxRetries: 30,
    retryDelay: 100,
  });
}

test('real Chromium process loss restarts the same Project profile without ChatGPT navigation', {
  skip: !realProjectBrowserTestsEnabled,
}, async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-real-restart-'));
  const projectId = 'g-p-real-restart';
  const projectUrl = 'https://chatgpt.com/g/g-p-real-restart/project';
  let first;
  let second;
  let firstHandle;
  let secondHandle;
  let firstProfileDigest;
  try {
    first = new ProjectBrowserManager({ projectId, projectUrl, machineRuntimeRoot: root });
    firstHandle = await first.acquire();
    const firstPid = firstHandle.record.process_id;
    firstProfileDigest = firstHandle.record.profile_path_digest;
    await first.release(firstHandle);
    firstHandle = null;
    await killRealBrowserProcess(firstPid);

    second = new ProjectBrowserManager({ projectId, projectUrl, machineRuntimeRoot: root });
    secondHandle = await second.acquire();
    assert.equal(secondHandle.reused, false);
    assert.notEqual(secondHandle.record.process_id, firstPid);
    assert.equal(secondHandle.record.profile_path_digest, firstProfileDigest);
    assert.equal(secondHandle.evidence.failure_classification, PROJECT_BROWSER_PROCESS_LOST);
    assert.equal(secondHandle.evidence.recovery_classification, RECOVERABLE_INFRASTRUCTURE_FAILURE);
    assert.equal(secondHandle.evidence.restart_history.at(-1).recovered, true);
    await second.release(secondHandle);
    secondHandle = null;
    await second.shutdown();
  } finally {
    if (secondHandle) await second.release(secondHandle).catch(() => {});
    await second?.shutdown().catch(() => {});
    if (firstHandle) await first.release(firstHandle).catch(() => {});
    await first?.shutdown().catch(() => {});
    await removeRealTestRoot(root);
  }
});

test('real Chromium keeps A/B Project profiles and processes isolated', {
  skip: !realProjectBrowserTestsEnabled,
}, async () => {
  const root = await fs.mkdtemp(path.join(process.cwd(), '.test-project-browser-real-isolation-'));
  let a;
  let b;
  let handleA;
  let handleB;
  try {
    a = new ProjectBrowserManager({
      projectId: 'g-p-real-a',
      projectUrl: 'https://chatgpt.com/g/g-p-real-a/project',
      machineRuntimeRoot: root,
    });
    b = new ProjectBrowserManager({
      projectId: 'g-p-real-b',
      projectUrl: 'https://chatgpt.com/g/g-p-real-b/project',
      machineRuntimeRoot: root,
    });
    handleA = await a.acquire();
    handleB = await b.acquire();
    assert.notEqual(handleA.record.process_id, handleB.record.process_id);
    assert.notEqual(a.profileDir, b.profileDir);
    assert.notEqual(handleA.record.profile_path_digest, handleB.record.profile_path_digest);
    await a.release(handleA);
    handleA = null;
    await b.release(handleB);
    handleB = null;
    await a.shutdown();
    await b.shutdown();
  } finally {
    if (handleB) await b.release(handleB).catch(() => {});
    await b?.shutdown().catch(() => {});
    if (handleA) await a.release(handleA).catch(() => {});
    await a?.shutdown().catch(() => {});
    await removeRealTestRoot(root);
  }
});
