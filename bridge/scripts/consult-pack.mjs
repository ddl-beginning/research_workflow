#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import {
  BRIDGE_ROOT,
  CONVERSATION_MODES,
  consultOnce,
  DEFAULT_PROFILE_DIR,
  FAILURE_CODES,
  resolveResponseTimeoutMs,
} from '../src/bridge.mjs';
import {
  buildContextPack,
  CONTEXT_PACK_MODES,
  resolveContextPackAttachmentRoot,
} from '../src/context-pack.mjs';
import { buildReviewerPrompt, writeLocalReviewSummary } from '../src/dialogue-policy.mjs';

function readOption(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? undefined : process.argv[index + 1];
}

function usage() {
  console.error('Usage: npm run consult-pack -- --spec path/to/spec.json [--project-url https://chatgpt.com/<project-path>] [--profile-dir PATH] [--timeout-ms N]');
}

const specPath = readOption('--spec');
if (!specPath) {
  usage();
  process.exitCode = 1;
} else {
  try {
    const spec = JSON.parse(await fs.readFile(path.resolve(specPath), 'utf8'));
    if (!spec || typeof spec !== 'object' || !spec.pack || typeof spec.question !== 'string') {
      throw new Error('A spec needs pack and question fields.');
    }
    const rootDir = path.resolve(spec.root_dir || BRIDGE_ROOT);
    const contextMode = spec.context_mode || spec.pack.mode || CONTEXT_PACK_MODES.NORMAL;
    const conversationMode = spec.conversation_mode || CONVERSATION_MODES.FRESH;
    const pack = await buildContextPack({
      ...spec.pack,
      rootDir,
      mode: contextMode,
    });
    // Context-pack attachments are created under the pack's canonical staging
    // boundary.  Pass that verified, narrow root explicitly so a supervisor
    // pack built in its disposable .tmp workspace is accepted without
    // allowing the whole workspace (or arbitrary direct paths).
    const contextPackAttachmentRoot = await resolveContextPackAttachmentRoot(pack);
    const prompt = buildReviewerPrompt({
      question: spec.question,
      mode: contextMode,
      packetId: pack.packetId,
    });
    const timeoutValue = readOption('--timeout-ms');
    const responseTimeoutMs = timeoutValue === undefined ? undefined : resolveResponseTimeoutMs(timeoutValue);
    const profileDir = path.resolve(readOption('--profile-dir') || DEFAULT_PROFILE_DIR);
    const projectUrl = readOption('--project-url') ?? spec.project_url;
    const transport = spec.transport === 'homepage_fallback' ? 'homepage_fallback' : undefined;
    const result = await consultOnce(prompt, {
      rootDir,
      profileDir,
      mode: conversationMode,
      ...(spec.continue_from === undefined ? {} : { continueFrom: spec.continue_from }),
      ...(projectUrl === undefined ? {} : { projectUrl }),
      contextPack: pack,
      ...(transport ? { transport } : {}),
      allowedAttachmentRoots: [contextPackAttachmentRoot],
      ...(Number.isFinite(responseTimeoutMs) && responseTimeoutMs > 0 ? { responseTimeoutMs } : {}),
      log: (message) => console.log(message),
    });
    const summaryPath = await writeLocalReviewSummary({ consultationDir: result.consultationDir, responseText: result.responseText });
    console.log(`packet_id=${pack.packetId}`);
    console.log(`packet_manifest=${pack.manifestPath}`);
    console.log(`packet_attachment_count=${pack.attachmentCount}`);
    console.log(`consultation_id=${result.consultationId}`);
    console.log(`conversation_id=${result.conversationId}`);
    if (result.projectUrl) console.log(`project_url=${result.projectUrl}`);
    if (result.pre_prompt_recovery) {
      console.log(`pre_prompt_recovery=${JSON.stringify(result.pre_prompt_recovery)}`);
    }
    console.log(`request_count=${result.requestCount}`);
    console.log(`receipt=${result.receiptPath}`);
    if (summaryPath) console.log(`review_summary=${summaryPath}`);
    console.log('CHATGPT_RESPONSE_BEGIN');
    console.log(result.responseText);
    console.log('CHATGPT_RESPONSE_END');
    console.log('CONTEXT_PACK_CONSULTATION_COMPLETE');
  } catch (error) {
    const code = error?.code || FAILURE_CODES.UNEXPECTED_PAGE_STATE;
    console.error(`CONTEXT_PACK_CONSULTATION_FAILED ${code}`);
    if (error?.prePromptRecovery) {
      console.error(`pre_prompt_recovery=${JSON.stringify(error.prePromptRecovery)}`);
    }
    if (error?.artifacts?.receiptPath) console.error(`receipt=${error.artifacts.receiptPath}`);
    if (error?.message) console.error(`${code} ${error.message}`);
    if (process.env.BRIDGE_DEBUG_ERRORS === '1' && error?.stack) console.error(error.stack.slice(0, 4000));
    process.exitCode = 1;
  }
}
