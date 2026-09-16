#!/usr/bin/env node

import process from 'node:process';
import {
  ProjectBrowserError,
  shutdownProjectBrowser,
} from '../src/project-browser-manager.mjs';

function readOption(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? undefined : process.argv[index + 1];
}

if (!process.argv.includes('--shutdown')) {
  console.error('Usage: node scripts/project-browser.mjs --shutdown --project-id PROJECT_ID [--project-url URL]');
  process.exitCode = 1;
} else {
  try {
    const projectId = readOption('--project-id');
    const projectUrl = readOption('--project-url');
    const stopped = await shutdownProjectBrowser({ projectId, projectUrl });
    console.log(stopped ? 'PROJECT_BROWSER_STOPPED' : 'PROJECT_BROWSER_NOT_FOUND');
  } catch (error) {
    const code = error instanceof ProjectBrowserError ? error.code : 'PROJECT_BROWSER_SHUTDOWN_FAILED';
    console.error(`${code} ${error?.message || 'Project Browser shutdown failed.'}`);
    process.exitCode = 1;
  }
}
