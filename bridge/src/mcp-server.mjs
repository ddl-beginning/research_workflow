import path from 'node:path';
import process from 'node:process';
import { pathToFileURL } from 'node:url';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import * as z from 'zod/v4';
import {
  loadContextPack,
  CONTEXT_PACKET_ID_PATTERN,
} from './context-pack.mjs';
import {
  asBridgeError,
  BridgeError,
  consultOnce,
  CONVERSATION_MODES,
  FAILURE_CODES,
  MAX_RESPONSE_TIMEOUT_MS,
  MAX_CHATGPT_REQUESTS_PER_INVOCATION,
  isValidProjectUrl,
  PROJECT_URL_MAX_CHARS,
} from './bridge.mjs';

export const MCP_SERVER_NAME = 'chatgpt-browser-bridge';
export const MCP_SERVER_VERSION = '0.5.0';
export const CONSULT_GPT_TOOL_NAME = 'consult_gpt';
export const MIN_TIMEOUT_MS = 5_000;
export const MAX_TIMEOUT_MS = MAX_RESPONSE_TIMEOUT_MS;
export const MAX_PROMPT_CHARS = 100_000;
export const MAX_CONTINUE_FROM_CHARS = 128;
export const MAX_ATTACHMENTS = 9;
export const MAX_CONTEXT_PACK_ID_CHARS = 128;

const FAILURE_CODE_VALUES = Object.values(FAILURE_CODES);
const KNOWN_FAILURE_CODES = new Set(FAILURE_CODE_VALUES);

export const consultGptInputSchema = z.object({
  prompt: z.string()
    .max(MAX_PROMPT_CHARS)
    .refine((value) => value.trim().length > 0, 'prompt must be non-empty'),
  timeout_ms: z.number()
    .int()
    .min(MIN_TIMEOUT_MS)
    .max(MAX_TIMEOUT_MS)
    .optional(),
  mode: z.enum([CONVERSATION_MODES.FRESH, CONVERSATION_MODES.CONTINUE]).default(CONVERSATION_MODES.FRESH),
  continue_from: z.string()
    .max(MAX_CONTINUE_FROM_CHARS)
    .refine((value) => value.trim().length > 0, 'continue_from must be non-empty')
    .optional(),
  attachments: z.array(z.string().min(1)).max(MAX_ATTACHMENTS).optional(),
  context_pack_id: z.string()
    .max(MAX_CONTEXT_PACK_ID_CHARS)
    .regex(CONTEXT_PACKET_ID_PATTERN)
    .optional(),
  project_url: z.string()
    .max(PROJECT_URL_MAX_CHARS)
    .refine((value) => isValidProjectUrl(value), 'project_url must be a safe https://chatgpt.com target')
    .optional(),
}).strict().superRefine((value, context) => {
  if (value.mode === CONVERSATION_MODES.FRESH && value.continue_from !== undefined) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['continue_from'],
      message: 'fresh mode must not include continue_from',
    });
  }
  if (value.mode === CONVERSATION_MODES.CONTINUE && value.continue_from === undefined) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['continue_from'],
      message: 'continue mode requires continue_from',
    });
  }
});

export const consultGptOutputSchema = z.object({
  status: z.enum(['complete', 'failed']),
  consultation_id: z.string(),
  response: z.string(),
  receipt_path: z.string(),
  request_count: z.number().int().min(0).max(MAX_CHATGPT_REQUESTS_PER_INVOCATION),
  failure_code: z.enum(FAILURE_CODE_VALUES).optional(),
}).strict();

function failureDetails(error) {
  const bridgeError = asBridgeError(error);
  const code = KNOWN_FAILURE_CODES.has(bridgeError.code)
    ? bridgeError.code
    : FAILURE_CODES.UNEXPECTED_PAGE_STATE;
  const consultationId = typeof error?.consultationId === 'string'
    ? error.consultationId
    : typeof bridgeError.consultationId === 'string' ? bridgeError.consultationId : '';
  const receiptPath = typeof error?.artifacts?.receiptPath === 'string'
    ? error.artifacts.receiptPath
    : typeof bridgeError.artifacts?.receiptPath === 'string' ? bridgeError.artifacts.receiptPath : '';
  const message = code === bridgeError.code && typeof bridgeError.message === 'string'
    ? bridgeError.message
    : 'The browser bridge failed unexpectedly.';
  return { code, message, consultationId, receiptPath };
}

function successResult(result) {
  if (result?.requestCount !== MAX_CHATGPT_REQUESTS_PER_INVOCATION) {
    throw new BridgeError(
      FAILURE_CODES.UNEXPECTED_PAGE_STATE,
      'The bridge returned an invalid request count.',
    );
  }
  const structuredContent = {
    status: 'complete',
    consultation_id: result.consultationId,
    response: result.responseText,
    receipt_path: result.receiptPath,
    request_count: MAX_CHATGPT_REQUESTS_PER_INVOCATION,
  };
  return {
    content: [{ type: 'text', text: result.responseText }],
    structuredContent,
  };
}

function failureResult(error) {
  const failure = failureDetails(error);
  const requestCount = Number.isInteger(error?.requestCount)
    ? Math.max(0, Math.min(MAX_CHATGPT_REQUESTS_PER_INVOCATION, error.requestCount))
    : Number.isInteger(error?.artifacts?.receipt?.request_count)
      ? Math.max(0, Math.min(MAX_CHATGPT_REQUESTS_PER_INVOCATION, error.artifacts.receipt.request_count))
      : failure.code.startsWith('CONTEXT_PACK_') ? 0 : MAX_CHATGPT_REQUESTS_PER_INVOCATION;
  const structuredContent = {
    status: 'failed',
    consultation_id: failure.consultationId,
    response: '',
    receipt_path: failure.receiptPath,
    request_count: requestCount,
    failure_code: failure.code,
  };
  return {
    content: [{ type: 'text', text: `${failure.code}: ${failure.message}` }],
    structuredContent,
    isError: true,
  };
}

export function createConsultGptHandler({ consult = consultOnce } = {}) {
  if (typeof consult !== 'function') throw new TypeError('consult must be a function');
  return async (rawArgs) => {
    const args = consultGptInputSchema.parse(rawArgs);
    try {
      const contextPack = args.context_pack_id === undefined
        ? undefined
        : await loadContextPack({ rootDir: process.cwd(), packetId: args.context_pack_id });
      const result = await consult(args.prompt, {
        ...(args.timeout_ms === undefined ? {} : { responseTimeoutMs: args.timeout_ms }),
        mode: args.mode,
        ...(args.continue_from === undefined ? {} : { continueFrom: args.continue_from }),
        ...(args.attachments === undefined ? {} : { attachments: args.attachments }),
        ...(contextPack === undefined ? {} : { contextPack }),
        ...(args.project_url === undefined ? {} : { projectUrl: args.project_url }),
      });
      return successResult(result);
    } catch (error) {
      return failureResult(error);
    }
  };
}

export function createMcpServer({ consult = consultOnce } = {}) {
  const server = new McpServer(
    {
      name: MCP_SERVER_NAME,
      version: MCP_SERVER_VERSION,
    },
    {
      instructions: 'This server exposes exactly one tool. Each call sends at most one prompt through the existing headed ChatGPT bridge using either an explicitly fresh conversation or a receipt-validated continue conversation. An optional project_url scopes fresh navigation and must match the parent receipt for continuation. Optional local attachments and an explicitly named staged context packet are restricted by server-side bridge roots and verified before that one prompt. It does not scan repositories, choose files, execute recommendations, or run autonomous loops.',
    },
  );
  server.registerTool(CONSULT_GPT_TOOL_NAME, {
    title: 'Consult ChatGPT once',
    description: 'Send one prompt with an optional explicit ChatGPT project URL, server-authorized local attachments, or an explicitly named staged context packet through the locally authenticated ChatGPT browser profile in a fresh or receipt-validated continue conversation, and return the complete assistant response plus a receipt path.',
    inputSchema: consultGptInputSchema,
    outputSchema: consultGptOutputSchema,
  }, createConsultGptHandler({ consult }));
  return server;
}

export async function startMcpServer({ consult = consultOnce } = {}) {
  const server = createMcpServer({ consult });
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`${MCP_SERVER_NAME} ready on stdio; tool=${CONSULT_GPT_TOOL_NAME}`);
  return { server, transport };
}

const isMainModule = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

if (isMainModule) {
  startMcpServer().catch((error) => {
    const failure = failureDetails(error);
    console.error(`${failure.code}: ${failure.message}`);
    process.exitCode = 1;
  });
}
