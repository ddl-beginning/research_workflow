#!/usr/bin/env node

import path from 'node:path';
import process from 'node:process';
import {
  consultOnce,
  DEFAULT_PROFILE_DIR,
  FAILURE_CODES,
  resolveResponseTimeoutMs,
} from '../src/bridge.mjs';

function readOption(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) return undefined;
  return process.argv[index + 1];
}

function readOptions(name) {
  const values = [];
  for (let index = 0; index < process.argv.length; index += 1) {
    if (process.argv[index] === name && process.argv[index + 1] !== undefined) values.push(process.argv[index + 1]);
  }
  return values;
}

function usage() {
  console.error('Usage: npm run consult -- --prompt "Reply with exactly: BRIDGE_OK" [--project-url https://chatgpt.com/g/g-p-.../project] [--attachment PATH ...] [--mode fresh|continue] [--continue-from CONSULTATION_ID]');
}

const prompt = readOption('--prompt');
if (!prompt) {
  usage();
  process.exitCode = 1;
} else {
  const profileDir = path.resolve(readOption('--profile-dir') || DEFAULT_PROFILE_DIR);
  const timeoutValue = readOption('--timeout-ms');
  const mode = readOption('--mode') || 'fresh';
  const continueFrom = readOption('--continue-from');
  const projectUrl = readOption('--project-url');
  const attachments = readOptions('--attachment');
  const log = (message) => console.log(message);
  console.log('CONSULTATION_STARTED');
  console.log(`profile=${profileDir}`);

  try {
    const responseTimeoutMs = timeoutValue === undefined ? undefined : resolveResponseTimeoutMs(timeoutValue);
    const result = await consultOnce(prompt, {
      profileDir,
      mode,
      ...(continueFrom === undefined ? {} : { continueFrom }),
      ...(projectUrl === undefined ? {} : { projectUrl }),
      ...(attachments.length === 0 ? {} : { attachments }),
      ...(Number.isFinite(responseTimeoutMs) && responseTimeoutMs > 0 ? { responseTimeoutMs } : {}),
      log,
    });
    console.log('CHATGPT_RESPONSE_BEGIN');
    console.log(result.responseText);
    console.log('CHATGPT_RESPONSE_END');
    console.log(`consultation_id=${result.consultationId}`);
    console.log(`mode=${result.mode}`);
    if (result.projectUrl) console.log(`project_url=${result.projectUrl}`);
    console.log(`conversation_id=${result.conversationId}`);
    console.log(`request=${result.requestPath}`);
    console.log(`response=${result.responsePath}`);
    console.log(`receipt=${result.receiptPath}`);
    console.log('CONSULTATION_COMPLETE');
  } catch (error) {
    const code = error?.code || FAILURE_CODES.UNEXPECTED_PAGE_STATE;
    console.error(`CONSULTATION_FAILED ${code}`);
    if (error?.artifacts?.receiptPath) console.error(`receipt=${error.artifacts.receiptPath}`);
    if (code === FAILURE_CODES.LOGIN_REQUIRED) {
      console.error(`LOGIN_REQUIRED Open the headed Chromium window using profile ${profileDir}.`);
      console.error('Log in manually once, close that window, then rerun npm run consult -- --prompt "<same prompt>".');
      // The browser is intentionally not closed by consultOnce so the user can complete login.
      process.exit(1);
    } else if (error?.message) {
      console.error(`${code} ${error.message}`);
    }
    process.exitCode = 1;
  }
}
