# Architecture

```text
local project_id → explicit ChatGPT project_url
                 → bridge project-route validation
                 → ProjectBrowserManager
                 → one headed Chromium process + isolated persistent profile
                 → ChatGPT Project UI
                 → fresh/receipt-validated conversation
                 → one prompt + sanitized receipt
```

本地项目与 ChatGPT Project 的绑定由调用方显式提供 `project_url`；bridge 不做 project discovery、blueprint、repo scanning 或自动项目管理。URL 只用于受限的浏览器 UI 导航：origin 必须是 `https://chatgpt.com`，路径必须是明确的 `/g/g-p-.../project` route。

fresh 的顺序是：acquire Project Browser lease → reuse or start that Project's
browser/profile → 必要时做一次 homepage bootstrap → 导航并验证 project URL →
检查登录 → 调用既有 `createFreshConversation` → 发送至多一条 prompt。
continue 先读取并验证 bridge receipt/schema/lineage，继承 parent project
binding（若存在），验证 project route 后再导航 receipt 的 `chat_url`；
conversation identity 必须保持不变。

正常 consultation 结束时只释放 lease 并断开 CDP；不关闭 Chromium。一个
healthy Project Browser 会被后续同 Project consultation 复用。不同显式
`project_id` 使用不同 registry/profile/process，显式 profile 冲突会在发送
prompt 前 fail-closed。

项目页的 `page.goto` 只在 timeout、暂时性 network 或 target-closed page-load 异常后做一次有界 navigation-only retry；HTTP `status >= 400`（包括 403 challenge）直接 fail-closed。导航诊断仅写入有界耗时、status、清洗后的 title/landing URL、分类和错误 hash，不增加 prompt request 计数。

receipt 中的 project metadata 只有规范化 URL、requested/verified 布尔值和受限的初始导航/parent binding evidence。sanitizer 不复制 cookie、token、storage 或任意页面 dump。旧 receipt 缺少 project scope 时仍兼容原有 fresh/continue 规则；新增 project scope 的 continue 不允许跨绑定 project。

`src/bridge.mjs` 拥有 URL、scope、receipt、lineage 和 one-request contract；`src/mcp-server.mjs` 只暴露 strict `project_url` schema 并传递到 bridge；`scripts/consult.mjs` 与 `scripts/consult-pack.mjs` 只负责 CLI 参数传递。这里不声称存在 ChatGPT 官方 Project API；官方文档层面的项目内聊天/记忆与从项目创建聊天能力，不改变该 UI route 的实现细节属性。
