# Quickstart

本 bridge 每次 consultation 仍只发送一条 prompt，但显式 Project 会复用一个由
`ProjectBrowserManager` 拥有的长期 Chromium 进程。每个本地项目应显式配置自己的
Workflow `project_id` 与 ChatGPT Project URL，避免多个项目共用进程、profile 或根路径的新对话。

## 1. 配置项目绑定

为本地项目保存一个明确的 ChatGPT Project URL，例如：

```text
https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project
```

该 URL 是浏览器 UI navigation implementation detail。bridge 只接受 `https://chatgpt.com/g/g-p-.../project`，不接受普通 chat URL、外域、相对路径、query/hash 或 credentials；它不是 ChatGPT 官方 API。

## 2. 运行一次 fresh consultation

```powershell
npm run consult -- --project-url "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project" --prompt "Reply exactly: PROJECT_OK"
```

首次 consultation 会为该 Project 在 machine runtime 下创建独立 profile，并在需要时打开
`https://chatgpt.com/` 完成 session/bootstrap；后续 healthy consultation 直接复用该
Project Browser，再导航/验证绑定的 Project URL。首页只用于 bootstrap，绝不作为 prompt
目标；成功 receipt 还会记录受限的 process/profile reuse evidence。

## 3. 继续同一 conversation

把成功 receipt 的 consultation id 与同一个 project URL 传回：

```powershell
npm run consult -- --mode continue --continue-from CONSULT-YYYYMMDD-HHMMSS-xxxxxxxx --project-url "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project" --prompt "Reply exactly: CONTINUE_OK"
```

continue 只打开 bridge receipt 的 `chat_url`。若 parent receipt 已绑定 project，省略 `--project-url` 时会继承该绑定；若显式传入，必须与 parent 完全一致。跨项目或无效 URL 会在发送 prompt 前 fail-closed，`request_count` 保持为 0。没有 project scope 的旧 receipt 仍可按原有规则继续。

MCP 调用使用 `project_id` 与 `project_url` 输入字段；它与 CLI 共享上述校验和 receipt lineage 规则。
