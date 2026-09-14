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
  return index === -1 ? undefined : process.argv[index + 1];
}

function readOptions(name) {
  const values = [];
  for (let index = 0; index < process.argv.length - 1; index += 1) {
    if (process.argv[index] === name) values.push(process.argv[index + 1]);
  }
  return values;
}

const profileDir = path.resolve(readOption('--profile-dir') || DEFAULT_PROFILE_DIR);
const attachmentPaths = readOptions('--attachment')
  .filter((value) => typeof value === 'string' && value.trim())
  .map((value) => path.resolve(value));
const timeoutValue = readOption('--response-timeout-ms');

async function runSmokePrompt(prompt, label, runOptions) {
  console.log(`CONSULTATION_STARTED ${label}`);
  const result = await consultOnce(prompt, runOptions);
  console.log(`CHATGPT_RESPONSE_BEGIN ${label}`);
  console.log(result.responseText);
  console.log(`CHATGPT_RESPONSE_END ${label}`);
  console.log(`request=${result.requestPath}`);
  console.log(`response=${result.responsePath}`);
  console.log(`receipt=${result.receiptPath}`);
  return result;
}

try {
  const responseTimeoutMs = timeoutValue === undefined ? undefined : resolveResponseTimeoutMs(timeoutValue);
  const options = {
    profileDir,
    ...(responseTimeoutMs === undefined ? {} : { responseTimeoutMs }),
    log: (message) => console.log(message),
  };
  const first = await runSmokePrompt('Reply with exactly: BRIDGE_OK', 'BRIDGE_OK', options);
  if (!first.responseText.includes('BRIDGE_OK')) {
    throw new Error('BRIDGE_OK was not present in the first assistant response.');
  }

  const second = await runSmokePrompt('Reply with exactly these three lines:\nTEST_A\nTEST_B\nTEST_C', 'TEST_A/B/C', options);
  if (
    second.responseText.length < 'TEST_A\nTEST_B\nTEST_C'.length ||
    !['TEST_A', 'TEST_B', 'TEST_C'].every((line) => second.responseText.includes(line))
  ) {
    throw new Error('The second assistant response did not contain all three test lines.');
  }

  if (attachmentPaths.length > 0) {
    const attachment = await runSmokePrompt(
      'Reply with exactly: ATTACHMENT_SMOKE_OK',
      'ATTACHMENT_SMOKE',
      { ...options, attachments: attachmentPaths },
    );
    if (attachment.responseText.trim() !== 'ATTACHMENT_SMOKE_OK') {
      throw new Error('The optional attachment smoke response was not exact.');
    }
    console.log('BROWSER_BRIDGE_ATTACHMENT_PASS');
  }

  console.log('BROWSER_BRIDGE_ROUNDTRIP_PASS');
} catch (error) {
  const code = error?.code || FAILURE_CODES.UNEXPECTED_PAGE_STATE;
  if (code === FAILURE_CODES.LOGIN_REQUIRED) {
    console.error(`LOGIN_REQUIRED Open the headed Chromium window using profile ${profileDir}.`);
    console.error('Log in manually once, close that window, then rerun npm run smoke.');
    process.exit(1);
  } else {
    console.error(`BROWSER_BRIDGE_ROUNDTRIP_NOT_READY ${code}`);
    if (error?.message) console.error(`${code} ${error.message}`);
  }
  process.exitCode = 1;
}
