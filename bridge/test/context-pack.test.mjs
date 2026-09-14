import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  buildContextPack,
  computeContextPackHash,
  CONTEXT_PACK_FAILURE_CODES,
  CONTEXT_PACK_MODES,
  CONTEXT_PACK_SCHEMA_VERSION,
  findAbsolutePath,
  loadContextPack,
  resolveContextPackAttachmentRoot,
  validateContextPackForConsult,
  validateContextManifest,
} from '../src/context-pack.mjs';
import {
  CONVERSATION_MODES,
  consultOnce,
  FAILURE_CODES,
  prepareAttachments,
  resolveAllowedAttachmentRoots,
  validateAttachmentPath,
} from '../src/bridge.mjs';
import {
  buildReviewerPrompt,
  parseLocalReviewSummary,
} from '../src/dialogue-policy.mjs';

const BRIDGE_ROOT = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const FIXTURE_ROOT = path.join(BRIDGE_ROOT, 'test', 'fixture_project');
const CONVERSATION_ID = '12345678-1234-4234-8234-123456789abc';

function basePackOptions(rootDir = BRIDGE_ROOT) {
  return {
    rootDir,
    evidenceRoots: [FIXTURE_ROOT],
    mode: CONTEXT_PACK_MODES.NORMAL,
    createdAt: '2026-09-04T00:00:00.000Z',
    projectGoal: 'Preserve a visible shoreline boundary while suppressing speckle.',
    currentStageGoal: 'Determine whether the erosion representation removes the thin boundary.',
    userVisibleGoal: 'Produce a stable boundary result that is reviewable at the target scale.',
    establishedFacts: ['The after result has fewer speckle components.', 'The upper-right boundary is visibly thinner.'],
    currentMethod: 'Fixed-radius binary erosion precedes contour extraction.',
    currentBlocker: 'The cleanup improvement trades away a boundary segment.',
    protectedForbiddenScope: ['Do not edit the real facade project.', 'Do not scan files not listed as evidence.'],
    hardConstraints: ['At most nine attachments.', 'One prompt per consultation invocation.'],
    previousRelevantDecisions: [
      { text: 'Keep the fixture small and compare observable outputs.', kind: 'decision', source: 'codex' },
      { text: 'Try a larger erosion radius.', kind: 'gpt_recommendation', source: 'gpt' },
    ],
    previousRecommendation: 'Try a larger erosion radius.',
    latestResult: {
      actualWork: 'Ran the fixed-radius cleanup on the fixture and selected the before/after outputs.',
      tested: ['Deterministic unit tests', 'Fixture metrics comparison'],
      success: ['Speckle components fell from 37 to 8.'],
      failure: ['Boundary IoU fell from 0.84 to 0.79.'],
      change: 'The output is cleaner but loses a thin upper-right boundary.',
      whyConsult: 'The next route should distinguish a representation problem from a parameter problem.',
      localReferences: ['test/fixture_project/CURRENT_RESULT.md'],
    },
  };
}

async function makeRoot(prefix = '.test-context-pack-') {
  return fs.mkdtemp(path.join(BRIDGE_ROOT, prefix));
}

async function removeRoot(rootDir) {
  await fs.rm(rootDir, { recursive: true, force: true });
}

test('context manifest follows schema, has repo-relative paths, and stays within nine attachments', async () => {
  const rootDir = await makeRoot();
  try {
    const pack = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-schema-0001',
      evidence: [
        { logicalName: 'review-before.png', sourcePath: path.join(FIXTURE_ROOT, 'result_before.png'), role: 'review' },
        { logicalName: 'review-after.png', sourcePath: path.join(FIXTURE_ROOT, 'result_after.png'), role: 'review' },
        { logicalName: 'fixture-metrics.json', sourcePath: path.join(FIXTURE_ROOT, 'metrics.json'), role: 'metric' },
        { logicalName: 'fixture-source.py', sourcePath: path.join(FIXTURE_ROOT, 'src', 'example.py'), role: 'source' },
      ],
    });
    assert.equal(pack.manifest.schema_version, CONTEXT_PACK_SCHEMA_VERSION);
    assert.equal(validateContextManifest(pack.manifest), true);
    assert.equal(pack.manifest.attachment_count, pack.attachmentPaths.length);
    assert.ok(pack.manifest.attachment_count <= 9);
    assert.match(pack.manifest.source_git_root_id, /^repo-/);
    assert.doesNotMatch(await fs.readFile(pack.manifestPath, 'utf8'), /[A-Za-z]:[\\/]/);
    assert.ok(pack.manifest.files.every((item) => !path.isAbsolute(item.relative_staged_path)));
    const loaded = await loadContextPack({ rootDir: rootDir, packetId: pack.packetId });
    assert.equal(loaded.manifestSha256, pack.manifestSha256);
    assert.equal(loaded.mode, pack.mode);
    assert.equal(loaded.attachmentCount, pack.attachmentCount);
  } finally {
    await removeRoot(rootDir);
  }
});

test('latest result renders codex_result summary, structured tests, and evidence references', async () => {
  const rootDir = await makeRoot();
  try {
    const pack = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-latest-result-0001',
      latestResult: {
        schema_version: 'codex_result.v1',
        summary: 'Unified Entry E2E complete.',
        tests: [
          { name: 'targeted', status: 'PASS', summary: '24 passed' },
          { name: 'full_regression', status: 'PASS', summary: '372 passed' },
        ],
        evidence_refs: ['.research/stages/unified-workflow-entry-v1/execution-evidence.json'],
      },
    });
    const latestResultText = await fs.readFile(path.join(pack.stagingDir, 'LATEST_RESULT.md'), 'utf8');
    assert.match(latestResultText, /Unified Entry E2E complete\./);
    assert.match(latestResultText, /"name": "targeted"/);
    assert.match(latestResultText, /"status": "PASS"/);
    assert.match(latestResultText, /"summary": "24 passed"/);
    assert.match(latestResultText, /\.research\/stages\/unified-workflow-entry-v1\/execution-evidence\.json/);
    assert.doesNotMatch(latestResultText, /\[object Object\]/);
  } finally {
    await removeRoot(rootDir);
  }
});

test('supervisor-origin FRESH pack uses only its canonical staging root and rejects an outside file', async () => {
  // Keep the source root outside the bridge checkout to reproduce a
  // supervisor .tmp pack without touching a real project or browser profile.
  const externalParent = path.dirname(BRIDGE_ROOT);
  const supervisorRoot = await fs.mkdtemp(path.join(externalParent, '.test-supervisor-origin-pack-'));
  const outsideRoot = await fs.mkdtemp(path.join(externalParent, '.test-supervisor-origin-outside-'));
  try {
    const pack = await buildContextPack({
      rootDir: supervisorRoot,
      packetId: 'PACK-supervisor-origin-0001',
      mode: CONTEXT_PACK_MODES.FRESH,
      createdAt: '2026-09-06T00:00:00.000Z',
      projectGoal: 'Keep the supervisor review bounded.',
      currentStageGoal: 'Verify a supervisor-origin context pack can cross the bridge boundary.',
      userVisibleGoal: 'Review one bounded architecture result.',
      establishedFacts: ['The pack is generated in a disposable supervisor workspace.'],
      currentMethod: 'Explicit bounded context pack.',
      currentBlocker: 'The bridge default root is different from the supervisor workspace.',
      protectedForbiddenScope: ['Do not include browser profile material.'],
      hardConstraints: ['At most nine attachments.', 'One prompt per invocation.'],
      latestResult: { actualWork: 'Prepared one bounded FRESH review packet.' },
      evidence: [{ logicalName: 'review.md', content: 'bounded supervisor evidence\n', role: 'review' }],
    });
    const stagingRoot = await resolveContextPackAttachmentRoot(pack);
    assert.equal(
      stagingRoot,
      await fs.realpath(path.join(supervisorRoot, '.consultations', 'staging')),
    );
    assert.ok(pack.attachmentPaths.every((value) => value.startsWith(`${stagingRoot}${path.sep}`)));

    const allowedRoots = await resolveAllowedAttachmentRoots({ allowedAttachmentRoots: [stagingRoot] });
    const prepared = await prepareAttachments(pack.attachmentPaths, { allowedAttachmentRoots: allowedRoots });
    assert.equal(prepared.files.length, pack.attachmentPaths.length);

    const outsideFile = path.join(outsideRoot, 'outside.txt');
    await fs.writeFile(outsideFile, 'must stay outside the pack staging root\n', 'utf8');
    await assert.rejects(
      validateAttachmentPath(outsideFile, { allowedAttachmentRoots: allowedRoots }),
      (error) => error.code === FAILURE_CODES.ATTACHMENT_ROOT_VIOLATION,
    );
  } finally {
    await removeRoot(supervisorRoot);
    await removeRoot(outsideRoot);
  }
});

test('realistic fixture before and after PNG evidence is deterministic and distinct', async () => {
  const before = await fs.readFile(path.join(FIXTURE_ROOT, 'result_before.png'));
  const after = await fs.readFile(path.join(FIXTURE_ROOT, 'result_after.png'));
  const digest = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
  assert.equal(before.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(after.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.notEqual(digest(before), digest(after));
});

test('input ordering is deterministic and duplicate logical files are deduplicated', async () => {
  const rootA = await makeRoot();
  try {
    const evidence = [
      { logicalName: 'z.txt', content: 'z', role: 'source' },
      { logicalName: 'a.txt', content: 'a', role: 'source' },
      { logicalName: 'z.txt', content: 'z', role: 'source' },
    ];
    const first = await buildContextPack({ ...basePackOptions(rootA), packetId: 'PACK-order-0001', evidence });
    const second = await buildContextPack({ ...basePackOptions(rootA), packetId: 'PACK-order-0002', evidence: evidence.slice().reverse() });
    assert.deepEqual(first.manifest.files.map((item) => item.logical_name), ['LATEST_RESULT.md', 'STAGE_CONTEXT.md', 'a.txt', 'z.txt']);
    assert.equal(first.manifest.files.length, 4);
    assert.equal(first.packHash, second.packHash);
    assert.deepEqual(first.manifest.files.map((item) => item.sha256), second.manifest.files.map((item) => item.sha256));
  } finally {
    await removeRoot(rootA);
  }
});

test('conflicting duplicate logical files fail closed', async () => {
  const rootDir = await makeRoot();
  try {
    await assert.rejects(
      buildContextPack({
        ...basePackOptions(rootDir),
        packetId: 'PACK-duplicate-0001',
        evidence: [
          { logicalName: 'same.txt', content: 'first', role: 'source' },
          { logicalName: 'same.txt', content: 'second', role: 'source' },
        ],
      }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.DUPLICATE_LOGICAL_FILE,
    );
  } finally {
    await removeRoot(rootDir);
  }
});

test('context pack enforces the nine-attachment cap including generated context files', async () => {
  const rootDir = await makeRoot();
  try {
    const sevenEvidence = Array.from({ length: 7 }, (_, index) => ({
      logicalName: `evidence-${index}.txt`,
      content: `evidence-${index}`,
      role: 'source',
    }));
    const accepted = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-cap-0001',
      evidence: sevenEvidence,
    });
    assert.equal(accepted.attachmentCount, 9);
    await assert.rejects(
      buildContextPack({
        ...basePackOptions(rootDir),
        packetId: 'PACK-cap-0002',
        evidence: [...sevenEvidence, { logicalName: 'evidence-7.txt', content: 'evidence-7', role: 'source' }],
      }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.ATTACHMENT_COUNT_EXCEEDED,
    );
  } finally {
    await removeRoot(rootDir);
  }
});

test('secret content is rejected before staging and is never redacted automatically', async () => {
  const rootDir = await makeRoot();
  try {
    await assert.rejects(
      buildContextPack({
        ...basePackOptions(rootDir),
        packetId: 'PACK-secret-0001',
        latestResult: { actualWork: 'password = "do-not-send-this"' },
      }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED,
    );
    await assert.rejects(fs.access(path.join(rootDir, '.consultations', 'staging', 'PACK-secret-0001')));
  } finally {
    await removeRoot(rootDir);
  }
});

test('sourcePath scans bounded bytes even for binary extensions and media types', async () => {
  const rootDir = await makeRoot();
  try {
    await fs.writeFile(
      path.join(rootDir, 'opaque-result.bin'),
      Buffer.from('opaque bytes: api_key = "binary-fixture-secret"\n', 'utf8'),
    );
    await assert.rejects(
      buildContextPack({
        ...basePackOptions(rootDir),
        evidenceRoots: [rootDir],
        packetId: 'PACK-binary-secret-001',
        evidence: [{
          logicalName: 'opaque-result.bin',
          sourcePath: 'opaque-result.bin',
          mediaType: 'application/octet-stream',
          role: 'result',
        }],
      }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED,
    );
    await assert.rejects(fs.access(path.join(rootDir, '.consultations', 'staging', 'PACK-binary-secret-001')));
  } finally {
    await removeRoot(rootDir);
  }
});

test('binary source path scan ignores random drive-like bytes outside printable runs', async () => {
  const rootDir = await makeRoot();
  try {
    await fs.writeFile(
      path.join(rootDir, 'compressed-result.png'),
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0x43, 0x3a, 0x2f, 0x00, 0x9a, 0xff]),
    );
    const pack = await buildContextPack({
      ...basePackOptions(rootDir),
      evidenceRoots: [rootDir],
      packetId: 'PACK-binary-drive-bytes-001',
      evidence: [{
        logicalName: 'compressed-result.png',
        sourcePath: 'compressed-result.png',
        mediaType: 'image/png',
        role: 'review',
      }],
    });
    assert.ok(pack.manifest.files.some((item) => item.logical_name === 'compressed-result.png'));
  } finally {
    await removeRoot(rootDir);
  }
});

test('inline content preflight applies to binary media types and catches secrets, paths, and JWTs', async () => {
  const rootDir = await makeRoot();
  try {
    const cases = [
      ['inline-api-key.bin', 'api_key = "inline-secret-material"', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-bearer.bin', 'Authorization: Bearer abcdefghijklmnop', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-private-key.bin', '-----BEGIN RSA PRIVATE KEY-----', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-jwt.bin', 'eyJaaaaaaaaaa.eyJbbbbbbbbbb.eyJcccccccccc', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-json-api-key.bin', '{"api_key":"inline-json-secret"}', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-json-password.bin', "{'password':'inline-json-secret'}", CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-json-session-id.bin', '{"session_id":"inline-json-session"}', CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-cookie-dump.bin', "{cookies:[{name:'session',value:'inline-cookie-secret'}]}", CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-storage-state.bin', "{storageState:{cookies:[{name:'session',value:'inline-storage-secret'}]}}", CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-local-storage.bin', "{localStorage:{token:'inline-storage-secret'}}", CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED],
      ['inline-drive-path.bin', String.raw`repo-C:\Users\foo\result.png`, CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED],
      ['inline-file-uri.bin', 'file:///C:/Users/foo/result.png', CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED],
      ['inline-unc-path.bin', String.raw`\\server\share\result.png`, CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED],
      ['inline-srv-path.bin', '/srv/project/secret.txt', CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED],
    ];
    for (const [index, [logicalName, content, expectedCode]] of cases.entries()) {
      await assert.rejects(
        buildContextPack({
          ...basePackOptions(rootDir),
          packetId: `PACK-inline-preflight-${String(index).padStart(3, '0')}`,
          evidence: [{ logicalName, content, mediaType: 'application/octet-stream', role: 'review' }],
        }),
        (error) => error.code === expectedCode,
      );
    }
  } finally {
    await removeRoot(rootDir);
  }
});

test('stable JSON metrics reject credential fields and cookie/storage dumps', async () => {
  const rootDir = await makeRoot();
  try {
    const cases = [
      { api_key: 'metrics-secret' },
      { access_token: 'metrics-secret' },
      { password: 'metrics-secret' },
      { session_id: 'metrics-session' },
      { cookies: [{ name: 'session', value: 'metrics-cookie' }] },
      { storageState: { cookies: [{ name: 'session', value: 'metrics-storage' }] } },
      { localStorage: { token: 'metrics-local-storage' } },
    ];
    for (const [index, metrics] of cases.entries()) {
      await assert.rejects(
        buildContextPack({
          ...basePackOptions(rootDir),
          packetId: `PACK-metrics-secret-${String(index).padStart(3, '0')}`,
          metrics,
        }),
        (error) => error.code === CONTEXT_PACK_FAILURE_CODES.SECRET_REJECTED,
      );
    }
  } finally {
    await removeRoot(rootDir);
  }
});

test('fresh packets exclude previous GPT recommendations while normal packets may retain them', async () => {
  const rootNormal = await makeRoot();
  const rootFresh = await makeRoot();
  try {
    const normal = await buildContextPack({ ...basePackOptions(rootNormal), packetId: 'PACK-normal-0001' });
    const freshOptions = basePackOptions(rootFresh);
    delete freshOptions.previousRecommendation;
    const fresh = await buildContextPack({
      ...freshOptions,
      packetId: 'PACK-fresh-0001',
      mode: CONTEXT_PACK_MODES.FRESH,
    });
    const normalText = await fs.readFile(path.join(normal.stagingDir, 'STAGE_CONTEXT.md'), 'utf8');
    const freshText = await fs.readFile(path.join(fresh.stagingDir, 'STAGE_CONTEXT.md'), 'utf8');
    assert.match(normalText, /larger erosion radius/i);
    assert.doesNotMatch(freshText, /larger erosion radius/i);
    assert.match(freshText, /Keep the fixture small/i);
  } finally {
    await removeRoot(rootNormal);
    await removeRoot(rootFresh);
  }
});

test('fresh context rejects explicitly named previous recommendation or chain fields', async () => {
  const rootDir = await makeRoot();
  try {
    for (const field of ['previous_recommendation', 'previous_chain', 'recommendation']) {
      await assert.rejects(
        buildContextPack({
          ...basePackOptions(rootDir),
          packetId: `PACK-fresh-reject-${field.replaceAll('_', '')}`,
          mode: CONTEXT_PACK_MODES.FRESH,
          [field]: 'do not carry this into fresh review',
        }),
        (error) => error.code === CONTEXT_PACK_FAILURE_CODES.INVALID,
      );
    }
  } finally {
    await removeRoot(rootDir);
  }
});

test('normal delta omits unchanged context and records reusable files', async () => {
  const rootDir = await makeRoot();
  try {
    const first = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-delta-a001',
      evidence: [{ logicalName: 'RESULT_A.md', content: 'IoU=0.84', role: 'result' }],
    });
    const second = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-delta-b001',
      latestResult: { ...basePackOptions().latestResult, change: 'Only the new result changed.' },
      evidence: [{ logicalName: 'RESULT_B.md', content: 'IoU=0.79', role: 'result' }],
      previousPack: first,
    });
    assert.deepEqual(second.manifest.files.map((item) => item.logical_name), ['LATEST_RESULT.md', 'RESULT_B.md']);
    assert.equal(second.manifest.attachment_count, 2);
    assert.ok(second.manifest.reused_files.some((item) => item.logical_name === 'STAGE_CONTEXT.md'));
    assert.ok(!second.attachmentPaths.some((filePath) => filePath.endsWith('STAGE_CONTEXT.md')));
    const third = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-delta-c001',
      latestResult: { ...basePackOptions().latestResult, change: 'Only the new result changed.' },
      evidence: [{ logicalName: 'RESULT_C.md', content: 'IoU=0.81', role: 'result' }],
      previousPack: second,
    });
    assert.deepEqual(third.manifest.files.map((item) => item.logical_name), ['RESULT_C.md']);
    assert.equal(third.manifest.attachment_count, 1);
    assert.ok(third.manifest.reused_files.some((item) => item.logical_name === 'STAGE_CONTEXT.md'));
    assert.ok(third.manifest.reused_files.some((item) => item.logical_name === 'LATEST_RESULT.md'));
    assert.ok(!third.attachmentPaths.some((filePath) => filePath.endsWith('STAGE_CONTEXT.md')));
  } finally {
    await removeRoot(rootDir);
  }
});

test('absolute local paths are excluded from GPT-visible output', async () => {
  const rootDir = await makeRoot();
  try {
    assert.equal(findAbsolutePath('See https://example.com/path'), null);
    assert.ok(findAbsolutePath('/srv/project/secret.txt'));
    await assert.rejects(
      buildContextPack({
        ...basePackOptions(rootDir),
        packetId: 'PACK-absolute-0001',
        currentBlocker: 'Inspect C:\\Users\\Administrator\\secret.txt',
      }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.ABSOLUTE_PATH_REJECTED,
    );
  } finally {
    await removeRoot(rootDir);
  }
});

test('context-pack preflight fails before prompt and writes request_count zero', async () => {
  const rootDir = await makeRoot();
  let sent = 0;
  const fakeBridge = {
    requestCount: 0,
    async open() { throw new Error('browser must not open on failed preflight'); },
    async close() {},
    releaseForManualLogin() {},
  };
  try {
    const pack = await buildContextPack({ ...basePackOptions(rootDir), packetId: 'PACK-preflight-0001' });
    await fs.appendFile(path.join(pack.stagingDir, 'LATEST_RESULT.md'), '\npassword = "not allowed"\n');
    await assert.rejects(
      consultOnce('Review the selected result.', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'profile'),
        mode: CONVERSATION_MODES.FRESH,
        contextPack: pack,
        bridgeFactory: () => {
          sent += 1;
          return fakeBridge;
        },
      }),
      (error) => error.code === FAILURE_CODES.CONTEXT_PACK_INTEGRITY_FAILED,
    );
    assert.equal(sent, 0);
    const consultationDirs = (await fs.readdir(path.join(rootDir, '.consultations'), { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && entry.name.startsWith('CONSULT-'));
    assert.equal(consultationDirs.length, 1);
    const receipt = JSON.parse(await fs.readFile(path.join(rootDir, '.consultations', consultationDirs[0].name, 'receipt.json'), 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.status, 'failed_before_prompt');
  } finally {
    await removeRoot(rootDir);
  }
});

test('context-pack secret preflight maps to CONTEXT_PACK_SECRET_REJECTED with zero requests', async () => {
  const rootDir = await makeRoot();
  let bridgeCreated = 0;
  try {
    const pack = await buildContextPack({ ...basePackOptions(rootDir), packetId: 'PACK-secret-preflight' });
    const resultPath = path.join(pack.stagingDir, 'LATEST_RESULT.md');
    const resultText = `${await fs.readFile(resultPath, 'utf8')}\napi_key = "secret-material-must-stop"\n`;
    await fs.writeFile(resultPath, resultText, 'utf8');
    const manifest = structuredClone(pack.manifest);
    const item = manifest.files.find((entry) => entry.logical_name === 'LATEST_RESULT.md');
    item.bytes = Buffer.byteLength(resultText, 'utf8');
    item.sha256 = crypto.createHash('sha256').update(resultText).digest('hex');
    // Keep the staged manifest structurally valid so consult preflight reaches
    // the byte-level secret scanner. A separate integrity test above covers a
    // tampered payload/hash and must continue to fail as CONTEXT_PACK_INVALID
    // or CONTEXT_PACK_INTEGRITY_FAILED before content inspection.
    manifest.pack_sha256 = computeContextPackHash(manifest);
    const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
    await fs.writeFile(pack.manifestPath, manifestText, 'utf8');
    pack.manifest = manifest;
    pack.packHash = manifest.pack_sha256;
    pack.manifestSha256 = crypto.createHash('sha256').update(manifestText).digest('hex');
    await assert.rejects(
      consultOnce('Review this packet.', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'profile'),
        contextPack: pack,
        mode: CONVERSATION_MODES.FRESH,
        bridgeFactory: () => {
          bridgeCreated += 1;
          throw new Error('bridge must not be constructed');
        },
      }),
      (error) => error.code === FAILURE_CODES.CONTEXT_PACK_SECRET_REJECTED,
    );
    assert.equal(bridgeCreated, 0);
    const consultationDirs = (await fs.readdir(path.join(rootDir, '.consultations'), { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && entry.name.startsWith('CONSULT-'));
    const receipt = JSON.parse(await fs.readFile(path.join(rootDir, '.consultations', consultationDirs[0].name, 'receipt.json'), 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.failure_code, FAILURE_CODES.CONTEXT_PACK_SECRET_REJECTED);
  } finally {
    await removeRoot(rootDir);
  }
});

test('tampered binary staged bytes with synchronized hashes are still rejected before bridge', async () => {
  const rootDir = await makeRoot();
  let failure;
  let bridgeCreated = 0;
  try {
    const pack = await buildContextPack({
      ...basePackOptions(rootDir),
      packetId: 'PACK-binary-tamper',
      evidence: [{
        logicalName: 'opaque-result.png',
        sourcePath: path.join(FIXTURE_ROOT, 'result_before.png'),
        mediaType: 'image/png',
        role: 'review',
      }],
    });
    const resultPath = path.join(pack.stagingDir, 'evidence', 'opaque-result.png');
    const tamperedBytes = Buffer.concat([
      await fs.readFile(resultPath),
      Buffer.from('\napi_key = "tampered-binary-secret"\n', 'utf8'),
    ]);
    await fs.writeFile(resultPath, tamperedBytes);
    const manifest = structuredClone(pack.manifest);
    const item = manifest.files.find((entry) => entry.logical_name === 'opaque-result.png');
    item.bytes = tamperedBytes.byteLength;
    item.sha256 = crypto.createHash('sha256').update(tamperedBytes).digest('hex');
    manifest.pack_sha256 = computeContextPackHash(manifest);
    const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
    await fs.writeFile(pack.manifestPath, manifestText, 'utf8');
    pack.manifest = manifest;
    pack.packHash = manifest.pack_sha256;
    pack.manifestSha256 = crypto.createHash('sha256').update(manifestText).digest('hex');
    await assert.rejects(
      consultOnce('Review the binary evidence.', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'profile'),
        mode: CONVERSATION_MODES.FRESH,
        contextPack: pack,
        bridgeFactory: () => {
          bridgeCreated += 1;
          throw new Error('bridge must not be constructed for binary secret tamper');
        },
      }),
      (error) => {
        failure = error;
        return error.code === FAILURE_CODES.CONTEXT_PACK_SECRET_REJECTED;
      },
    );
    assert.equal(bridgeCreated, 0);
    assert.equal(failure.requestCount, 0);
    const receipt = JSON.parse(await fs.readFile(failure.artifacts.receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.failure_code, FAILURE_CODES.CONTEXT_PACK_SECRET_REJECTED);
  } finally {
    await removeRoot(rootDir);
  }
});

test('manifest hash and disk/in-memory packet identity tampering fail before bridge creation', async () => {
  const rootDir = await makeRoot();
  let failure;
  let bridgeCreated = 0;
  try {
    const pack = await buildContextPack({ ...basePackOptions(rootDir), packetId: 'PACK-manifest-tamper' });
    const forgedHash = { ...pack.manifest, pack_sha256: 'a'.repeat(64) };
    assert.equal(validateContextManifest(forgedHash), false);

    const diskManifest = { ...pack.manifest, mode: CONTEXT_PACK_MODES.FRESH };
    diskManifest.pack_sha256 = computeContextPackHash(diskManifest);
    const diskManifestText = `${JSON.stringify(diskManifest, null, 2)}\n`;
    await fs.writeFile(pack.manifestPath, diskManifestText, 'utf8');
    const tampered = {
      ...pack,
      // Keep the in-memory manifest valid/normal while making the on-disk
      // packet a separately valid fresh manifest. The validator must compare
      // the two canonical manifests instead of trusting either hash alone.
      manifestSha256: crypto.createHash('sha256').update(diskManifestText).digest('hex'),
    };
    await assert.rejects(
      validateContextPackForConsult(tampered),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.INTEGRITY_FAILED,
    );
    await assert.rejects(
      consultOnce('Review this packet.', {
        rootDir,
        profileDir: path.join(rootDir, '.auth', 'profile'),
        mode: CONVERSATION_MODES.FRESH,
        contextPack: tampered,
        bridgeFactory: () => {
          bridgeCreated += 1;
          throw new Error('bridge must not be constructed for manifest tamper');
        },
      }),
      (error) => {
        failure = error;
        return error.code === FAILURE_CODES.CONTEXT_PACK_INTEGRITY_FAILED;
      },
    );
    assert.equal(bridgeCreated, 0);
    assert.equal(failure.requestCount, 0);
    const receipt = JSON.parse(await fs.readFile(failure.artifacts.receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.failure_code, FAILURE_CODES.CONTEXT_PACK_INTEGRITY_FAILED);
  } finally {
    await removeRoot(rootDir);
  }
});

test('staging packet symlink escape fails closed before bridge creation', async (t) => {
  const safeRoot = await makeRoot('.test-context-safe-');
  const outsideRoot = await makeRoot('.test-context-outside-');
  let symlinkCreated = false;
  try {
    const freshOptions = basePackOptions(outsideRoot);
    delete freshOptions.previousRecommendation;
    const outsidePack = await buildContextPack({
      ...freshOptions,
      packetId: 'PACK-staging-escape',
      mode: CONTEXT_PACK_MODES.FRESH,
    });
    const safeStagingRoot = path.join(safeRoot, '.consultations', 'staging');
    await fs.mkdir(safeStagingRoot, { recursive: true });
    const escapedPacket = path.join(safeStagingRoot, outsidePack.packetId);
    try {
      await fs.symlink(outsidePack.stagingDir, escapedPacket, 'junction');
      symlinkCreated = true;
    } catch (error) {
      if (error?.code === 'EPERM' || error?.code === 'EACCES' || error?.code === 'UNKNOWN') {
        t.skip(`junction creation is unavailable: ${error.code}`);
        return;
      }
      throw error;
    }
    await assert.rejects(
      loadContextPack({ rootDir: safeRoot, packetId: outsidePack.packetId }),
      (error) => error.code === CONTEXT_PACK_FAILURE_CODES.SOURCE_ROOT_VIOLATION,
    );
    const escapedPack = {
      ...outsidePack,
      rootDir: safeRoot,
      stagingDir: escapedPacket,
      manifestPath: path.join(escapedPacket, 'context_manifest.json'),
      attachmentPaths: outsidePack.attachmentPaths.map((filePath) => (
        path.join(escapedPacket, path.relative(outsidePack.stagingDir, filePath))
      )),
    };
    let failure;
    let bridgeCreated = 0;
    await assert.rejects(
      consultOnce('Review this explicitly named packet.', {
        rootDir: safeRoot,
        profileDir: path.join(safeRoot, '.auth', 'profile'),
        mode: CONVERSATION_MODES.FRESH,
        contextPack: escapedPack,
        bridgeFactory: () => {
          bridgeCreated += 1;
          throw new Error('bridge must not be constructed for staging escape');
        },
      }),
      (error) => {
        failure = error;
        return error.code === FAILURE_CODES.CONTEXT_PACK_SOURCE_ROOT_VIOLATION;
      },
    );
    assert.equal(bridgeCreated, 0);
    assert.equal(failure.requestCount, 0);
    const receipt = JSON.parse(await fs.readFile(failure.artifacts.receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 0);
    assert.equal(receipt.failure_code, FAILURE_CODES.CONTEXT_PACK_SOURCE_ROOT_VIOLATION);
  } finally {
    if (symlinkCreated) {
      // The link itself is inside the disposable safe root; removeRoot does
      // not follow it, so the outside packet remains independently bounded.
    }
    await removeRoot(safeRoot);
    await removeRoot(outsideRoot);
  }
});

test('context-pack receipt provenance is safe and machine-readable', async () => {
  const rootDir = await makeRoot();
  const events = [];
  const fakeBridge = {
    requestCount: 0,
    async open() { events.push('open'); },
    async navigate() { return 'https://chatgpt.com/'; },
    async ensureLoggedIn() {},
    async createFreshConversation() { return { afterUrl: `https://chatgpt.com/c/${CONVERSATION_ID}` }; },
    async waitForAssistantBaseline() { return []; },
    async uploadPreparedAttachments() {},
    async sendOnePrompt() { this.requestCount += 1; events.push('send'); return 'REVIEW_OK'; },
    currentUrl() { return `https://chatgpt.com/c/${CONVERSATION_ID}`; },
    async close() {},
  };
  try {
    const pack = await buildContextPack({ ...basePackOptions(rootDir), packetId: 'PACK-receipt-0001' });
    const result = await consultOnce('Use the selected evidence and diagnose the fixture.', {
      rootDir,
      profileDir: path.join(rootDir, '.auth', 'profile'),
      mode: CONVERSATION_MODES.FRESH,
      contextPack: pack,
      bridgeFactory: () => fakeBridge,
    });
    const receipt = JSON.parse(await fs.readFile(result.receiptPath, 'utf8'));
    assert.equal(receipt.request_count, 1);
    assert.deepEqual(receipt.context_pack, {
      packet_id: pack.packetId,
      mode: 'normal',
      relative_manifest_path: `staging/${pack.packetId}/context_manifest.json`,
      manifest_sha256: pack.manifestSha256,
      pack_sha256: pack.packHash,
      attachment_count: pack.attachmentCount,
    });
    assert.doesNotMatch(JSON.stringify(receipt.context_pack), /[A-Za-z]:[\\/]/);
    assert.deepEqual(events, ['open', 'send']);
  } finally {
    await removeRoot(rootDir);
  }
});

test('dialogue policy provides explicit question and optional local summary parsing', () => {
  const prompt = buildReviewerPrompt({ question: 'Is the erosion representation the blocker?', mode: 'fresh', packetId: 'PACK-policy-0001' });
  assert.match(prompt, /current diagnosis/i);
  assert.match(prompt, /Is the erosion representation/i);
  assert.match(prompt, /independent architecture review/i);
  const summary = parseLocalReviewSummary(`\n\`\`\`json\n${JSON.stringify({
    diagnosis: 'The cleanup is too destructive.',
    recommended_route: 'Measure a scale-aware boundary directly.',
    next_action: 'Run one bounded comparison.',
    stop_or_replan_condition: 'Replan if the direct measurement is unstable.',
    stage_review_recommended: false,
  })}\n\`\`\`\n`);
  assert.equal(summary.stage_review_recommended, false);
});

test('pack hash input is stable for equivalent selected evidence', async () => {
  const rootA = await makeRoot();
  try {
    const options = basePackOptions();
    const first = await buildContextPack({ ...options, rootDir: rootA, packetId: 'PACK-hash-a001', evidence: [{ logicalName: 'a.txt', content: 'stable', role: 'source' }] });
    const second = await buildContextPack({ ...options, rootDir: rootA, packetId: 'PACK-hash-b001', evidence: [{ logicalName: 'a.txt', content: 'stable', role: 'source' }] });
    assert.equal(first.packHash, second.packHash);
    assert.equal(crypto.createHash('sha256').update(await fs.readFile(first.manifestPath)).digest('hex'), first.manifestSha256);
  } finally {
    await removeRoot(rootA);
  }
});
