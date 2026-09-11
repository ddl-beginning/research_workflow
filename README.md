# Stage-Oriented Research Supervisor PoC

## 真实 Bridge 配置与 E2E 交接

开始新的会话前，请先读取 [LOCAL_BRIDGE_HANDOFF.md](LOCAL_BRIDGE_HANDOFF.md)，以获取真实 Bridge 配置和 E2E 交接信息。根仓库缺少 `.research/PROJECT_BRIEF` 并不代表配置缺失。

这是一个完全本地、确定性的 Stage-Oriented Research Supervisor。它把一个研究 Stage 的 planner decision、baseline、有限 iteration、scoped Codex task/result、critic packet、review 和 Human Gate 放进一个小状态机中，并提供一个无后台循环的薄 CLI，方便在普通 Git 仓库中采用。

状态机本身不会启动 Codex CLI、worker、MCP connector、浏览器或公网 tunnel，也不会自动执行 GPT 建议。CLI 只在目标仓库的 `.research/` 下持久化 Stage 状态和有界咨询元数据；真实 ChatGPT Pro transport 仍由已有 `chatgpt_browser_bridge` 显式调用。

## 快速运行

项目只使用 Python 标准库（建议 Python 3.9+）：

```text
cd D:\work\research_tools\research_supervisor_poc
python -m unittest discover -s tests -v
```

如果环境中已经有 pytest，也可以运行：

```text
python -m pytest -q
```

没有 pytest 时，第一条 `unittest` 命令就是完整 fallback；PoC 不安装任何依赖。

## 最薄 CLI

在一个普通 Git 仓库中先创建 `.research/PROJECT.md` 和
`.research/stages/stage-001/contract.json`（可复制
`examples/project-adoption/` 作为结构示例），然后显式操作 Stage：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py prepare --contract .research/stages/stage-001/contract.json
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py start stage-001
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py show stage-001
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py consult stage-001 --evidence .research/latest-result.txt
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py fresh-review stage-001 --reason "closeout review"
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py show-artifacts stage-001
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py approve stage-001
```

省略 `--repo` 时使用当前目录；状态默认写入
`.research/stage-state.json`，咨询索引写入 `.research/reviews/index.json`。
`pause` 是 `stop` 的同义词，不会启动后台任务。`approve` 和 `reject` 只接受
`STAGE_READY`；拒绝会回到同一个 Stage 的 `ACTIVE` 并保留理由。下一 Stage
仍然是 `PLANNED`，必须再次显式 `start`。

CLI 的 `consult` 使用 NORMAL continuation（同一 Stage 有新 evidence 时自动
复用上一正常会话）；`fresh-review` 使用真正的 FRESH review，不继承旧的
recommendation。两者都只产生一次 bridge request；失败、状态不允许、路径
越界、重复 digest、缺少 artifact 或发现敏感字段时 fail closed。使用
`--dry-run` 可在不接触 ChatGPT 的情况下检查 consultation plan。

完成 Step 10 的本地回归（复用既有 Step 9 sanitized receipt，不发新请求）：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step10_acceptance.py --step9-receipt D:\Temp\step9-disposable-dps7na1n\.research\integration\stage-integration-3a2013109150.json
```

脚本运行 bridge 静态检查/测试、supervisor 全部测试、CLI help、schema 和
no-secret 检查，并确认 Step 9 receipt 的 `STAGE_READY`、`request_count=1`、
context-pack 上限与 `CODEX_GPT_STAGE_INTEGRATION_PASS`。缺少或不一致的
receipt 会输出 `STEP10_NOT_READY`；只有全部检查通过才输出
`CODEX_GPT_STAGE_WORKFLOW_PASS`。

## 目录

```text
schemas/
  stage_contract.schema.json       stage 输入合同
  stage_context_pack.schema.json   有界恢复上下文
  git_baseline.schema.json         Git baseline + dirty-tree digest
  stage_context.schema.json        bounded STAGE_CONTEXT.md contract
  artifact_manifest.schema.json    immutable review-artifact manifest
  planner_decision.schema.json     五态 planner decision
  stage_review.schema.json         ACCEPT/REJECT 人工 review
  codex_task.schema.json           scoped fake-Codex task
  codex_result.schema.json         scoped ResultReport
src/
  contracts.py                     schema loader、标准库 validator、scope/baseline helper
  git_baseline.py                  read-only Git baseline capture/persistence
  stage_context.py                 bounded context/evidence selection/delta layer
  artifacts.py                     hashed review-artifact registry
  supervisor.py                    单 stage 有限状态机和 policy helper
fixtures/                          fake planner/task/result/baseline 数据
tests/                             Scenario A–E 及合同/policy 检查
WORKFLOW_DEMO.md                   一条完整 fake flow
```

## 状态与决策

`Supervisor.start_stage()` 验证 `stage_contract.v1`，并用 `freeze_baseline()` 保存只读的 content-addressed baseline。每次 task 只能提交一个匹配的 iteration result；状态机不包含后台 loop。

Planner 每个结果只产生一个决策：

| 条件（优先级从高到低） | 决策 | stage 状态 | 通知 |
| --- | --- | --- | --- |
| ResultReport `status=BLOCKED` | `BLOCKED` | `BLOCKED` | blocked 事件 |
| 显式重大不确定性或 `human_gate_required` | `HUMAN_GATE` | `HUMAN_GATE` | Human Gate 事件 |
| architecture check 触发 | `REPLAN` | `ACTIVE` | 静默 |
| `stage_ready=true` | `STAGE_READY` | `STAGE_READY` | 等待 stage review |
| `FAILED`/`FALSIFIED` | `REPLAN` | `ACTIVE` | 静默 |
| 其余成功的普通 iteration | `CONTINUE` | `ACTIVE` | 静默 |

`accept_stage()` 只允许 `STAGE_READY`，并转为 `ACCEPTED`、记录 accepted milestone；`reject_stage()` 转回同一 stage 的 `ACTIVE`，不会创建 accepted milestone 或下一 stage，后续可显式创建下一 iteration。重大不确定性通过 `resolve_human_gate(approved=...)` 处理：批准恢复 `ACTIVE`，拒绝进入 `BLOCKED`。

### Architecture check

状态机会记录连续 iteration 的 `abstraction_layer`。默认规则是：同一层连续两次没有用户可见改善时，`architecture_check.triggered=true`、route 为 `REPLAN`（旧标签仅保留为 `legacy_route=ARCHITECTURE_RESET`），并产生静默 `REPLAN`；stage 的 `stage_id` 与 `user_visible_goal` 保持不变。计数小于阈值，或出现用户可见改善，都会打断连续计数。阈值可通过 `Supervisor(architecture_threshold=2..)` 显式设置；没有自动架构改写。

### Baseline、critic 与 context

- baseline 在 stage start 固化一次；`compare_to_baseline()` 返回逐 metric delta、变化项和候选 digest，不覆盖 baseline。
- `critic_mode=NONE` 不派生 critic task；重大 representation replacement 可切换 `comparison_mode=MAJOR_CHALLENGER`，只在同一 `representative_set` 生成 `primary` 与 `challenger` 两个描述，硬上限为 2；`select_winner()` 后只保留一个 active lane。
- `fresh_critic_packet()` 每个结果生成一个带 `packet_id`、`supersedes`、baseline/result digest 的新 packet，不复用上一轮证据。
- `build_stage_context_pack()` 只允许最多九类资源：`user_plan`、`stage_contract`、`baseline`、`latest_result`、`critic_packet`、`decision_history`、`stop_rules`、`acceptance`、`safety`。它不是 transcript；schema 同时声明了 `maxProperties=9`。

Step 8 的独立适配层由 `capture_git_baseline()`、`freeze_git_baseline()`、
`build_stage_context()` 和 `ArtifactRegistry` 提供。Git baseline 只执行只读
命令；dirty tree 记录状态和 content digest，但不会自动 commit、checkout 或
覆盖用户改动。baseline 文件按 digest 首次写入，后续 iteration 只能复用原值。
`build_stage_context()` 生成包含 Stage 关键字段的 bounded machine pack，并可
渲染为 `STAGE_CONTEXT.md`；超过九个 resource classes 时要么把完整溢出值放入
显式的 `compressed_evidence`，要么 fail closed。NORMAL delta 返回
`reused_files`，FRESH 不携带 previous GPT recommendation。ArtifactRegistry
支持 `representative_good`、`representative_hard`、`worst_case`、`before_after`
和 `metrics_summary`，文件类型不限于 PNG；manifest/content hash、primary 与
challenger lane 都会被校验且不会互相覆盖。

## Codex 合同与安全边界

`codex_task.v1` 强制携带 `plan_id`、`stage_id`、`task_id`、`allowed_paths`、`read_first`、`context_refs`、acceptance、measurement commands、stop rules、result schema 和 iteration。当前实现只接受 `lane=fake-codex`，并把 network/destructive actions 固定为 deny。

`codex_result.v1` 必须带 status、summary、changed files、tests、measurements、evidence refs、abstraction layer、scientific result 和 gate flags。提交时会再次核对 ID、iteration、abstraction layer，并拒绝位于 `allowed_paths` 之外的 changed file。路径不接受绝对路径或 `..` 穿越。

这些是 PoC 内的合同检查，不是 OS sandbox；不能把它当成真实 Codex 权限边界。真实部署仍需要 loopback、认证、显式 disposable workspace、网络隔离、一次性人工批准、日志与资源限制。普通 `CONTINUE`/`REPLAN` 返回 `notification=None`；只有 Human Gate、blocked 和人工 review 需要显式事件。

## 明确不在范围内

本目录不修改 `chatgpt-mcp-codex`、`facade_geometry` 或全局 Codex 配置，不复制 upstream 实现，不安装依赖，不启动真实 Codex/`--full-auto`，不启动 tunnel，也不提供无限循环或自动生产发布路径。CLI 的真实 bridge 咨询必须由用户显式运行并使用已有登录 profile；本目录的单元测试和状态机测试使用 fake bridge，测试结果只能证明本地状态合同与 CLI 边界的可复现性。

更多操作流程见 [QUICKSTART.md](QUICKSTART.md)，职责边界见
[ARCHITECTURE.md](ARCHITECTURE.md)，可复用 Codex instructions 见
`skills/stage-oriented-research-workflow/SKILL.md`。

## Step 11：Codex-native Requirements Intake

Step 11 在 Stage 之前提供独立的 project-level requirements intake。它只在
目标项目的 `.research/PROJECT_BRIEF.json` 保存一个 canonical brief，支持
`USER_CONFIRMED_BRIEF`（用户直接提供 brief）和
`CODEX_REQUIREMENTS_INTERVIEW`（Codex 一次只呈现一个核心问题）。仓库中可由
只读检查得到的事实会自动记录在 brief 的 `repository_facts` 中，不会被反问。

最薄入口可以使用独立脚本，也可以使用现有 Stage CLI 的同名命令：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-init --mode CODEX_REQUIREMENTS_INTERVIEW --rough-requirement "想做成这个工作流"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-show
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-answer --answer "希望用户能看到一个可验证的结果"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-update --field success_criteria=检查结果可复现
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-approve --rationale "已确认 brief"
python D:\work\research_tools\research_supervisor_poc\scripts\requirements_cli.py --repo <PROJECT> requirements-cancel --reason "停止该需求"
```

状态只有 `DRAFT`、`WAITING_USER_APPROVAL`、`APPROVED`、`CANCELLED`。初始化粗略
需求会进入 `DRAFT`/`NEEDS_MORE_USER_INPUT` 并输出一个 `question`；补齐目标和
至少一个验收条件后才会等待人工 approval。修改会保留同一个 `project_id`，而
已批准 brief 的修改会回到 approval gate。`approve` 不自动启动 Project
Discovery、Stage 或 GPT；`cancel` 是终态并 fail-closed。Step 11 代码内的
`gpt_calls` 与 `external_calls` 永远为 0，不调用真实 ChatGPT，也不保存
transcript、question 文件或重复 brief。

## Step 12：Context Compaction / Artifact Retention

Step 12 仍是独立的本地层，只接受已经显式 `APPROVED` 的
`.research/PROJECT_BRIEF.json`。`DRAFT`、`WAITING_USER_APPROVAL` 和
`CANCELLED` 都 fail-closed，不会创建 `.research/PROJECT_CONTEXT.md`。
`src/project_context.py` 生成一个确定性、原子写入、可重复运行的 canonical
context，最多选择九类资源，保留真实目标、输入输出、user-visible success、
约束、non-goals、偏好、决策、状态、selected/rejected route（短理由和
evidence ref）、未决问题、next action、artifact refs 和 Git identity；它
不复制 transcript、raw prompt、浏览器状态或响应。

`src/artifact_retention.py` 只允许清理 workflow-owned `EPHEMERAL` 路径，
并把 `CANONICAL`、`MILESTONE_EVIDENCE`、`ACTIVE_REFERENCE` 记录在
`.research/ARTIFACT_RETENTION_MANIFEST.json` 中。source/data、业务文件、
Git history、canonical 文件和 milestone evidence 都受保护。重复 compaction
不会创建版本化 context 文件。离线验收命令为：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step12_acceptance.py
```

## Step 13：Bounded Project Discovery

Step 13 先建立统一的 `.research/AVAILABLE_ASSETS.json`（`available_assets.v1`）。
它只记录需求、可选的本地项目 Candidate 0、已由本地 verifier 检查的外部候选，
以及有限的 baseline/evidence 元数据；每项资产都带完整的 kind/role、验证状态、
digest 和 upload policy。存在真实本地项目时，Codex 只读生成
`.research/LOCAL_PROJECT_PROFILE.md`，不会把整个 repository 自动附加或上传。
没有本地项目不是错误，需求和 context 资产仍可正常进入 Discovery。

Discovery 与 Blueprint 共用 bounded metadata-only Asset Pack。Candidate 0 和外部候选
使用同一 `candidate_evidence.v1` 事实标准，且都保持 `candidate_only`，不因来源而自动
优先。Step 14 的一个 primary route 可以显式使用
`KEEP_EXISTING`、`KEEP_AND_OPTIMIZE`、`KEEP_AND_BORROW`、`REPLACE_BASE`、
`BUILD_FROM_EXISTING_LIBRARIES` 或 `BUILD_NEW`，但仍只产生一个待用户 gate 的路线。

资产层的离线 marker 为 `AVAILABLE_ASSET_LAYER_PASS`；本地项目审计 marker 为
`LOCAL_PROJECT_CANDIDATE_PASS`。重复更新相同事实会复用 canonical bytes，不创建
`asset_audit_*.md` 或其他重复摘要文件。

Step 13 在已批准的 `.research/PROJECT_BRIEF.json`（以及可选、已验证的
`.research/PROJECT_CONTEXT.md`）之后提供一次显式的、`fresh` 模式的项目发现
咨询。它要求调用方注入一次性 consultant 和本地 repository verifier；默认不
连接浏览器或真实 GPT。consultant 必须收到 brief 中的
`chatgpt_project_url` 或 `chatgpt_project_binding.url`，缺失时 fail closed。
结果只保留一个 primary、最多四个 alternatives，并在本地验证候选后原子写入
`.research/discovery/DISCOVERY_REPORT.json`。重复 evidence digest、失败重试、
原始 prompt/response、transcript、credentials 和临时 clone 都不会被保留。
`no_direct_match_found` 是合法结果，Step 13 不启动 Stage、不选择生产架构、
也不修改业务源代码。

离线验收命令：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step13_acceptance.py
```

验收成功 marker 为 `GPT_PROJECT_DISCOVERY_PASS`；脚本只使用 fake consultant，
不会发起真实请求。Step 15 会在显式调用后消费这些 canonical outputs，生成
一个合法的 `stage_contract.v1` 并通过现有 Stage Controller 注册为 `PLANNED`。

## Step 14：CODEX Feasibility / Project Blueprint

Step 14 要求 Step 11 的 brief 已 `APPROVED`、Step 12 的
`.research/PROJECT_CONTEXT.md` 已验证、且 Step 13 的
`.research/discovery/DISCOVERY_REPORT.json` 已完成。它先在本地生成紧凑的
`CODEX_FEASIBILITY` packet，再通过显式注入的 consultant 做一次 `fresh`
review；验收使用 fake consultant，不调用真实 GPT。prompt 不携带前一轮
recommendation 或 sunk-cost 路线。

结果只输出一个 primary route 和最多四个 alternatives，原子写入
`.research/blueprint/PROJECT_BLUEPRINT.md` 及其
`.research/blueprint/BLUEPRINT_MANIFEST.json`，状态为
`PROJECT_BLUEPRINT_READY`，下一动作是 `READY_FOR_STAGE_PLANNING`。Step 14
不会创建或启动 Stage，也不会修改业务源代码；相同证据 digest 会复用现有
canonical artifact。离线验收命令：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step14_acceptance.py
```

成功 marker 为 `GPT_CODEX_BLUEPRINT_REVIEW_PASS`。

## Step 15：Bootstrap-to-Stage Planning

Step 15 是从已验证 Bootstrap 产物到第一个可审阅 Stage 的本地规划层。
`src/stage_planning.py` 验证已批准的 `PROJECT_BRIEF.json`、`PROJECT_CONTEXT.md`、
Discovery report、Blueprint manifest/Markdown 和 `bootstrap_state.json` 的项目、
digest、边界与就绪状态，然后根据 Blueprint 的唯一 primary route 确定性生成
`stage_contract.v1`。它复用 `StageController.prepare_stage` 注册 Stage，因此
`.research/stage-state.json` 中的状态为 `PLANNED`；不会调用 bridge、执行 route、
修改业务/源代码或自动进入 `ACTIVE`。

```text
python D:\work\research_tools\research_supervisor_poc\scripts\stage_cli.py --repo PATH plan-stage
```

canonical outputs 为 `.research/stages/<stage-id>/contract.json`、
`.research/stage-planning/STAGE_PLAN.json` 和 `.research/stage-state.json`。规划
envelope 使用 `STAGE_PLANNING_PASS` marker，记录输入文件 digest、baseline digest、
contract digest、路径和下一步 `USER_START_STAGE`。相同输入会复用现有计划，输入发生
变化或证据缺失则 fail closed。离线验收命令为：

```text
python D:\work\research_tools\research_supervisor_poc\scripts\step15_acceptance.py
```
