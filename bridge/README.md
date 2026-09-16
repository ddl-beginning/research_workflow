# chatgpt_browser_bridge

这是一个只验证浏览器通信的最小 one-shot transport PoC：

~~~text
Codex/CLI → Playwright → 独立 headed Chromium profile → ChatGPT Web
~~~

本目录独立于业务仓库。它不启动 HTTP server；只有用户显式运行 Layer 2 命令时才启动本地 stdio MCP server。不读取或输出密码、cookie、token、storage dump，也不修改全局 Codex/MCP 配置。

## 当前明确支持

- 使用 `.auth/chatgpt-profile/` 的独立 persistent headed Chromium profile。
- 首次未登录时返回 `LOGIN_REQUIRED`，用户在可见窗口手动登录后重跑命令。
- 每次 CLI invocation 最多发送一条纯文本 user prompt；代码常量 `MAX_CHATGPT_REQUESTS_PER_INVOCATION=1` 强制该限制。
- 发送前记录 assistant 消息 baseline；只接受发送后新增、带稳定 DOM 标识的单个 assistant message。若生成中的临时 placeholder 被 ChatGPT 替换为最终节点，只要仍在同一 logical assistant slot 内会继续等待；出现第二个 slot 则失败。
- 等待生成状态结束并经过短 stability window；hard timeout 时返回 `RESPONSE_TIMEOUT`，不会把 partial response 当成功。
- 成功输出 `CONSULTATION_STARTED`、`CHATGPT_RESPONSE_BEGIN/END`、`CONSULTATION_COMPLETE`，并写入本地 consultation receipt。
- `fresh` 新建对话、`continue` 仅通过 bridge receipt 继续已验证对话，并保持 fresh-room isolation。
- 可选用显式 `project_url`（CLI 的 `--project-url`）把本地项目绑定到一个 ChatGPT Project，例如 `https://chatgpt.com/g/g-p-example-project/project`；fresh 先落到该 project route、检查登录，再创建 fresh conversation。
- project URL 只接受 `https://chatgpt.com/g/g-p-.../project` 这一明确 UI route；任意外域、普通 chat URL、相对/模糊路径、query/hash 或 credentials 都会 fail-closed。continue 会继承 receipt 的 project binding，并拒绝跨项目 URL。
- 可选本地 TXT/文档/PDF/PNG 等附件；使用 Playwright 原生 file upload。附件必须位于 bridge server-side allowed root，最多 9 个、单文件最多 8 MiB（这是本地 PoC safety cap，不是 ChatGPT 官方文件上限）。
- 上传会等待 composer tile/preview、目标 basename、无 pending/error 状态及可用 composer/send control；上传失败或未 ready 不发送 prompt。
- 上传前会先清理当前 composer 的旧 attachment tile；清理失败或仍有可见残留时立即 fail-closed，废弃本次 browser session，不继续上传或发送。
- receipt 的 `attachments` 元数据包含 `basename`、安全相对路径、字节数、SHA-256、媒体类型和 `upload_status`，不包含本地绝对路径或认证内容。
- attachment validator 会把实际 `profileDir` 的 canonical path 作为额外敏感边界；即使 profile 使用任意目录名，其中的 `Login Data`、`Preferences`、Cookies 等文件也不能作为附件。
- `.auth/` 和 `.consultations/` 已 gitignore；receipt 不含认证字段。
- 可由调用方显式选择证据并构造 `.consultations/staging/<packet_id>/` context pack；builder 不扫描仓库、不自动选择文件。
- context pack 支持 `normal` 与 `fresh` 两种 dialogue policy：normal continuation 可复用 unchanged evidence，fresh packet 排除上一轮 GPT recommendation。
- packet manifest 记录 schema、模式、repo-relative Git metadata、文件 hash/大小/角色和 attachment count；最多 9 个附件，receipt 只保存安全 packet provenance。
- outgoing packet 会在 staging 前检查高置信度 secret、敏感源路径和 GPT-visible absolute path；命中 `CONTEXT_PACK_SECRET_REJECTED` 等 failure code 时 fail-closed，不自动 redact。

## 当前明确不支持

本轮刻意不实现自动 repo scanning、automatic file selection、project manager、Git baseline、Stage Controller、自动 GPT consultation decisions、HTTP server/tunnel、自动登录、递归或 autonomous loop。context-pack 只接收调用方明确列出的 evidence。

## 已有依赖和安装前提

需要 Node.js 20+（`package.json` 与当前使用的 Playwright 1.62.1 均以 Node.js 20+ 为前提）。`playwright` 1.62.1、对应 `playwright-core` 和本 Step 2 新增的 `@modelcontextprotocol/sdk` 1.30.0 已在本目录的 `node_modules/` 中；本轮只为 MCP 包装安装了后者，没有下载 browser binary。依赖缺失的全新 checkout 可在本目录执行：

~~~powershell
npm install
~~~

不要把 `.auth/` 或 `.consultations/` 提交到 git。

## One-shot consultation

必须明确传一个非空 prompt：

~~~powershell
npm run consult -- --prompt "Reply with exactly: BRIDGE_OK"
~~~

默认 response hard timeout 为 180000 毫秒（3 分钟）；单次请求的硬上限为 300000 毫秒（5 分钟）。也可显式覆盖，但不得超过该上限：

~~~powershell
npm run consult -- --prompt "Reply with exactly: BRIDGE_OK" --timeout-ms 180000
~~~

默认会打开 headed Chromium，使用 profile `.auth/chatgpt-profile/`。如果终端出现 `LOGIN_REQUIRED`，请在该独立 Chromium 窗口中手动完成 ChatGPT 登录；不要将账号、密码或验证码传给脚本。登录完成后关闭窗口，再重跑原来的 `npm run consult -- --prompt ...` 命令。

CLI 成功退出码为 0；任何失败退出码均非 0。失败码包括（Layer 1/Layer 3）：

- `LOGIN_REQUIRED`
- `CHATGPT_NAVIGATION_FAILED`
- `PROMPT_INPUT_NOT_FOUND`
- `PROMPT_SEND_FAILED`
- `RESPONSE_TIMEOUT`
- `RESPONSE_EXTRACTION_FAILED`
- `UNEXPECTED_PAGE_STATE`
- `CONTINUATION_RECEIPT_NOT_FOUND`
- `CONTINUATION_RECEIPT_INVALID`
- `CONTINUATION_CHAT_NOT_FOUND`
- `FRESH_CHAT_CREATION_FAILED`
- `CONVERSATION_IDENTITY_MISMATCH`
- `PROJECT_URL_INVALID`
- `PROJECT_SCOPE_MISMATCH`
- `PROJECT_NAVIGATION_FAILED`
- `ATTACHMENT_INVALID`
- `ATTACHMENT_NOT_FOUND`
- `ATTACHMENT_NOT_REGULAR_FILE`
- `ATTACHMENT_NOT_READABLE`
- `ATTACHMENT_ROOT_VIOLATION`
- `ATTACHMENT_SENSITIVE_PATH_DENIED`
- `ATTACHMENT_COUNT_EXCEEDED`
- `ATTACHMENT_SIZE_EXCEEDED`
- `ATTACHMENT_UPLOAD_FAILED`
- `ATTACHMENT_NOT_READY`
- `CONTEXT_PACK_INVALID`
- `CONTEXT_PACK_ALREADY_EXISTS`
- `CONTEXT_PACK_SOURCE_NOT_FOUND`
- `CONTEXT_PACK_SOURCE_ROOT_VIOLATION`
- `CONTEXT_PACK_SOURCE_NOT_REGULAR_FILE`
- `CONTEXT_PACK_SOURCE_SIZE_EXCEEDED`
- `CONTEXT_PACK_DUPLICATE_LOGICAL_FILE`
- `CONTEXT_PACK_ATTACHMENT_COUNT_EXCEEDED`
- `CONTEXT_PACK_SECRET_REJECTED`
- `CONTEXT_PACK_ABSOLUTE_PATH_REJECTED`
- `CONTEXT_PACK_MANIFEST_INVALID`
- `CONTEXT_PACK_MANIFEST_NOT_FOUND`
- `CONTEXT_PACK_INTEGRITY_FAILED`

成功和失败都会在 `.consultations/<consultation_id>/` 写入：

- `request.txt`：本次用户 prompt
- `response.txt`：完整新 assistant response；失败时为空，不保存 partial response
- `receipt.json`：`consultation_id`、`created_at`、`mode=fresh|continue`、实际 `request_count`（成功为 1，prompt 发送前失败为 0）、`profile`、最终 `chat_url`、`conversation_id`、`parent_consultation_id`、`conversation_root_consultation_id`、`conversation_validated`、`status`、`response_char_count`，失败时另含 `failure_code`；有附件时另含安全 `attachments` 元数据。使用 project scope 时另含 `project_url`、`project_scope_requested`、`project_scope_verified` 和仅含初始落点布尔/规范化 URL 的 `project_scope_evidence`；项目导航还可记录受限的 `project_navigation_diagnostics`（attempt/retry 次数、有界耗时、HTTP status、清洗后的 title/landing URL、failure class 和错误 hash）。不写入 DOM、cookie、token 或 storage 内容。

## Layer 2：Codex 本地 stdio MCP

Layer 1 的 CLI transport 已经通过真实 headed smoke；Layer 2 只把它包装成一个很薄的 MCP server，不复制 Playwright selector、assistant extractor、generation wait 或 browser lifecycle。

显式启动（stdio；stdout 保留给 MCP JSON-RPC，诊断日志走 stderr）：

~~~powershell
npm run mcp
# 等价于：node src/mcp-server.mjs
~~~

只注册一个工具：`consult_gpt`。

- 输入：`prompt`（必填、非空、最多 100000 字符）；`timeout_ms`（可选整数，5000–300000 毫秒）；`mode`（可选 `fresh|continue`，默认 `fresh`）；`continue_from`（continue 模式必填的 consultation id）；`project_url`（可选、明确的 ChatGPT Project route）；`attachments`（可选本地路径数组，最多 9 个，可为空数组）；`context_pack_id`（可选、调用方明确指定的 `PACK-...` staging packet id）。
- 输入 schema 是 strict 的；fresh 不得带 `continue_from`，continue 不得缺少它；allowed roots 来自 bridge server-side 配置（默认 bridge root，也可用进程启动时的 `CHATGPT_ALLOWED_ATTACHMENT_ROOTS` 配置），`context_pack_id` 只解析当前 bridge root 下显式命名的 staging packet；不接受任意 chat URL、`profile_dir`、browser executable、cookie、token、password 或其它控制参数。
- 成功返回 MCP text content（完整 response）和 structured content：`status=complete`、`consultation_id`、`response`、`receipt_path`、`request_count=1`。
- 失败返回 `status=failed`、`isError=true` 和原样 `failure_code`（例如 `LOGIN_REQUIRED`、`RESPONSE_TIMEOUT`、`ATTACHMENT_ROOT_VIOLATION`），并尽可能附带 `consultation_id`、`receipt_path` 及实际 `request_count`；不会把 failure code 填进 `status`。任何 prompt 发送前 attachment failure 都是 `request_count=0`。
- `failure_code` 只会使用上面列出的 bridge、attachment 或 context-pack failure code 之一。
- 每次 tool call 仍调用既有 `consultOnce`，因此最多一条 ChatGPT prompt；不做 prompt retry、follow-up、clarification、递归或 autonomous loop。仅 project 页面加载允许一次 navigation-only retry，且不增加 `request_count`。

本轮没有永久注册 MCP server。Codex CLI 支持用一次性 `-c` 覆盖配置，可在不改全局配置的情况下做本地验证：

~~~powershell
codex exec --ignore-user-config -C D:\work\research_tools\chatgpt_browser_bridge `
  -c 'mcp_servers.consult_gpt.command="node"' `
  -c 'mcp_servers.consult_gpt.args=["src/mcp-server.mjs"]' `
  -c 'mcp_servers.consult_gpt.cwd="D:/work/research_tools/chatgpt_browser_bridge"' `
  -c 'mcp_servers.consult_gpt.startup_timeout_sec=30' `
  -c 'mcp_servers.consult_gpt.tool_timeout_sec=300' `
  'Use the consult_gpt MCP tool exactly once with prompt Reply with exactly: MCP_BRIDGE_OK, then report the returned response and receipt path.'
~~~

该命令不写入全局 `~/.codex/config.toml`；也不要把 `.auth/` 或 `.consultations/` 纳入 MCP 参数或提交。

## Layer 3：受 receipt 约束的 fresh/continue 对话

`consult_gpt` 的 `mode` 默认是 `fresh`。fresh 会在发送 prompt 前显式点击可访问的 New chat 控件；发送并完成后，只有当当前 ChatGPT URL 是受信任 origin 下的 `/c/<conversation_id>` 且 conversation identity 可验证时才成功。不能证明新 conversation 时返回 `FRESH_CHAT_CREATION_FAILED` 或 `CONVERSATION_IDENTITY_MISMATCH`，不会把旧房间当 fresh。

如果提供 `project_url`，fresh 会先导航到该明确 project route，验证实际初始落点仍是同一 project route，再检查登录并调用同一 `createFreshConversation`。项目页加载遇到 timeout、暂时性 network 或 target-closed 异常时，最多只对同一个 `page.goto` 做一次有界 settling 后重试；不会重试整个 consult、上传或 prompt，`request_count` 不增加。HTTP `status >= 400`（尤其 403 challenge）立即以 `PROJECT_NAVIGATION_FAILED` fail-closed，不盲重试。receipt 只保存规范化 project URL、初始落点证据和固定字段的导航诊断；title/URL 有界清洗并去除 query/hash/credentials，错误只保存 hash。这是浏览器 UI navigation implementation detail，不是 ChatGPT 官方 API 或稳定 API contract。continue 仍以 receipt 的 `chat_url` 为 conversation 目标；如果 parent receipt 有 project binding，调用方省略 URL 时继承该 binding，若提供 URL 则必须完全匹配，随后先验证该 project route 再打开 receipt chat。旧的无 project scope receipt 仍可按原规则继续。

continue 只接受 bridge 生成的 `continue_from=<consultation_id>`，从 `.consultations/<id>/receipt.json` 读取并校验 `status=complete`、`conversation_validated=true`、合法 ChatGPT URL、conversation id 和 receipt lineage；不接受任意 URL/path。缺失或越界返回 `CONTINUATION_RECEIPT_NOT_FOUND`，格式/字段/identity 不合格返回 `CONTINUATION_RECEIPT_INVALID`，目标房间无法打开返回 `CONTINUATION_CHAT_NOT_FOUND`。

每个 turn 都有独立 consultation id，但 receipt 会记录 `conversation_id`、`parent_consultation_id`、`conversation_root_consultation_id`、`conversation_validated` 和最终 `chat_url`。continue 成功前后必须保持 parent 的 conversation id；失败 receipt 会保留已知 lineage，但 `conversation_validated=false`，不会宣称对话 identity 已验证。使用 context pack 时 receipt 另含 `context_pack.packet_id`、`mode`、相对 manifest 路径、manifest/pack hash 和 attachment count，不含绝对本地路径。每次调用仍严格只发送一条 ChatGPT prompt。

CLI 示例：

~~~powershell
# 显式 fresh（省略 --mode 也等价）
npm run consult -- --mode fresh --prompt "Remember this nonce: CTX_demo. Reply exactly: TURN_A_OK"

# 将本地项目显式绑定到一个 ChatGPT Project URL
npm run consult -- --mode fresh --project-url "https://chatgpt.com/g/g-p-example-project/project" --prompt "Reply exactly: PROJECT_TURN_OK"

# 使用上一个成功 receipt 继续同一房间
npm run consult -- --mode continue --continue-from CONSULT-YYYYMMDD-HHMMSS-xxxxxxxx --project-url "https://chatgpt.com/g/g-p-example-project/project" --prompt "Reply with exactly the nonce I asked you to remember."

# 显式上传一个 server-side allowed root 下的文件
npm run consult -- --mode fresh --attachment "test/fixtures/attachment_nonce.txt" --prompt "Read the attached file and reply with exactly FIXTURE_ATTACHMENT_NONCE and nothing else."
~~~

## 本地测试和 headed smoke

不依赖浏览器的单元测试：

~~~powershell
npm run check
npm test
~~~

headed smoke 默认以两个独立 invocation 发送两条无害 prompt；每个 invocation 仍最多一条 prompt：

~~~powershell
npm run smoke
~~~

第一条精确请求 `BRIDGE_OK`；第二条请求三行 `TEST_A`、`TEST_B`、`TEST_C`，并检查响应不是短/缺行响应。两个都成功才输出 `BROWSER_BRIDGE_ROUNDTRIP_PASS`。登录 gate 输出 `LOGIN_REQUIRED`；其它失败输出 `BROWSER_BRIDGE_ROUNDTRIP_NOT_READY` 及具体 failure code；不会绕过登录或自动继续。

如需显式做一次附件 headed smoke，可额外传一个或多个本地路径；这只增加一次有界 invocation，不会自动循环、重试或无限创建 fresh room：

~~~powershell
npm run smoke -- --attachment "test/fixtures/attachment_nonce.txt"
~~~

该可选检查要求返回精确的 `ATTACHMENT_SMOKE_OK` 并记录 attachment receipt；安全边界和 failure-code matrix 由 `npm test` 覆盖。

## Context pack 与 dialogue policy

`src/context-pack.mjs` 是一个显式输入的 packet builder。调用方需要提供 goal、Stage 状态、latest result 和明确的 evidence descriptors；它不会扫描整个仓库或替调用方挑选文件。每个 packet 写入：

~~~text
.consultations/staging/<packet_id>/
  STAGE_CONTEXT.md
  LATEST_RESULT.md
  METRICS.json / REVIEW.png / WORST_CASE.png / DIFF_SUMMARY.md / SOURCE_CONTEXT.md  (optional)
  evidence/...                                                               (explicit files)
  context_manifest.json
~~~

`STAGE_CONTEXT.md` 固定包含 Project Goal、Current Stage Goal、User-visible Goal、Established Facts、Current Method、Current Blocker、Protected / Forbidden Scope、Relevant Previous Decisions。NORMAL 可保留相关历史决定；FRESH 要求调用方提供结构化的当前证据，并拒绝显式的 `previous_recommendation`、`recommendation`、`previous_chain` 等历史推荐/链字段；带有 `kind=gpt_recommendation` 或 `source=gpt` 的 previous decision 不会渲染。builder 不对任意自由文本做语义识别，调用方必须自行保证没有辩护性叙述或 sunk-cost narrative。NORMAL packet 可传 `previousPack`，unchanged logical files 记录在 `reused_files`，本轮 attachments 只保留变化项。

`context_manifest.json` 是 local provenance 和可选审计证据；GPT-visible 文件始终使用 repo-relative paths。manifest 与文本 evidence 在发送前校验 hash、路径和 secret。发现 API key、private key、bearer/access token、GitHub/Slack token、password、cookie/session 等高置信度内容时返回 `CONTEXT_PACK_SECRET_REJECTED`，不自动脱敏后继续。`LATEST_RESULT.md` 只摘要 Codex 实际动作、测试、成功/失败、结果变化和本次咨询原因，不直接复制长日志。

`consult-pack` 上传 context pack 时只显式允许该 pack 自己经过 canonical boundary 验证的 `.consultations/staging` 根目录。这样 pack 可以由 supervisor 在受控 disposable workspace 中生成，同时不会把整个 supervisor workspace 变成 attachment root；其它直接路径仍需 bridge server-side allowed roots 配置。

MCP `consult_gpt` 可用 `context_pack_id` 显式加载一个已 staging 的 packet；CLI 也提供：

~~~powershell
npm run consult-pack -- --spec path/to/explicit-pack-spec.json
~~~

`src/dialogue-policy.mjs` 和 [RESULT_GENERATION_REVIEW_POLICY.md](docs/RESULT_GENERATION_REVIEW_POLICY.md) 定义了非自动的结果生成/审查 agent policy：它要求 diagnosis、recommended route、next Codex action、stop/replan condition 和 stage-review suitability，并把观察、建议和不确定性分开。该 policy 不会触发咨询、扫描项目、执行 GPT 建议或启动递归循环。

可以用一次低频 headed smoke 验证 realistic fixture 的 NORMAL A → same-conversation B(delta) → independent FRESH：

~~~powershell
npm run context-smoke
~~~

该脚本不重试、不自动登录、不执行建议；若服务返回频率限制或登录 gate，应保留已有 receipt 并停止。

## 实现边界

`src/chatgpt-ui.mjs` 集中维护可访问 role、textarea/contenteditable、稳定 data attributes、attachment file input/tile/readiness/cleanup、assistant baseline、新消息 extractor、generation state 和 stability wait。`src/context-pack.mjs` 负责显式 evidence normalization、bounded staging、manifest/hash、NORMAL delta/FRESH isolation 和 outgoing preflight；`src/dialogue-policy.mjs` 负责非自动 prompt/policy 与 best-effort local summary；`src/project-browser-manager.mjs` 负责 machine-local one-project-one-process registry、profile isolation、consultation lease、reconnect/restart 与 explicit shutdown；`src/bridge.mjs` 负责 Project scope、attachment/context security boundary（含实际 profileDir canonical boundary）、一次发送预算、错误映射和 receipt，并在 consultation 完成时只 release Project Browser lease；`src/mcp-server.mjs` 只负责 MCP schema/handler/result mapping；`scripts/consult.mjs` 是 Layer 1 one-shot CLI，`scripts/consult-pack.mjs` 是显式 packet consultation CLI，`scripts/smoke-test.mjs` 默认编排两次独立测试 invocation，`scripts/context-pack-smoke.mjs` 仅在用户显式运行时做一次 A→B→fresh fixture smoke。

Layer 2/3 仍不提供自动 repo scanning、automatic file selection、project manager、Git baseline、Stage Controller、自动 GPT consultation decisions、HTTP/tunnel、自动登录或多轮自主决策能力。context-pack 仅处理调用方明确给出的 evidence。
