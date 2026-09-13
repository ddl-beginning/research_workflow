# V2 Spec Kit Adoption Decision

Stage: `spec-kit-adoption-and-repository-architecture-v1`。
状态：架构提案；未安装 Spec Kit，未执行 migration。最终需要 Human Architecture Gate。

## 研究依据

2026-09-12 实际打开官方 GitHub repository，并在项目外只读检出官方源码：
`https://github.com/github/spec-kit`，commit
`d848fb4e18f44640ad6b42e60a280551ee90cdce`。
研究副本位于机器临时目录，未作为 Workflow Engine dependency，也未运行其中命令。
以下链接均固定到这个 revision；结论不是仅凭旧版 Spec Kit 的模型记忆。

| 来源 | 阅读重点 |
|---|---|
| [README](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/README.md) | WHAT/WHY、HOW、任务、实现与 converge 的分工；项目初始化与模板扩展边界 |
| [Spec persistence](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/docs/concepts/spec-persistence.md) | Flow-back、Flow-forward、Living Spec 是团队约定；不是必须采用的 CLI 默认 |
| [Evolving specs](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/docs/guides/evolving-specs.md) | feature 演进与工具自身升级是两条维护流程；保留 rationale |
| [Constitution template](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/constitution-template.md) / [command](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/constitution.md) | 项目原则、治理、版本及变更影响同步 |
| [Spec template](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/spec-template.md) / [specify](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/specify.md) | 用户场景、独立验收、需求、可测结果；feature identity 不必等于分支名称 |
| [Clarify](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/clarify.md) | 先消除会改变实现或验收的重要歧义，答案写回 spec |
| [Plan template](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/plan-template.md) / [plan](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/plan.md) | 技术上下文、constitution check、研究与设计依据、实际目录结构 |
| [Tasks template](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/tasks-template.md) / [tasks](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/tasks.md) | 用户故事分组、稳定 ID、依赖、明确路径、独立交付；测试按需求而非机械生成 |
| [Analyze](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/analyze.md) | 实现前跨 artifact 覆盖、歧义、冲突检查，输出发现而非直接改写 |
| [Implement](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/implement.md) | 消费 plan/tasks，尊重依赖和 checklist，执行任务与验证 |
| [Converge](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/templates/commands/converge.md) | 检查当前实现与意图的差距，只追加可追溯任务，不重写旧任务、不修改应用代码 |

## 为什么这样分

Spec 面向问题和价值：用户是谁、希望发生什么、为什么值得做、如何验收。
Plan 面向实现选择：采用什么技术和边界、为何接受某个 trade-off、如何验证。
Tasks 面向可执行工作：把已接受的方向拆成具体路径、依赖和完成条件。
Constitution 是跨 feature 的治理约束，不能被某个局部 plan 悄悄推翻。
把这些责任分开，使技术变化不必重写产品目标，任务完成也不会被误当作需求已满足。

这里借鉴的是指导结构。Workflow V2 的执行许可、观察、评估、决策与关闭仍由
现有 controller protocol 决定；Markdown 中写了“完成”不能改变 Stage 状态。

## 决策矩阵

| Concept | Decision | Workflow V2 的具体采用方式与原因 |
|---|---|---|
| Constitution | ADOPT | 每项目一份稳定原则和变更规则，包含 lifecycle 冻结与 ownership 约束；不把产品规则复制进每个 Stage handoff |
| WHAT / WHY specification | ADOPT | `spec.md` 用场景、需求 ID、非目标、可观察成功标准承载 Human intent |
| Specify feature directories | ADAPT | 使用 canonical Stage ID 对应 `specs/<stage-id>/`；不强制三位编号或与 Git branch 同名；不建立第二个 feature identity authority |
| Clarify | ADAPT | 把高影响未知写回 spec；复用现有 Human intake/Decision resolver。已明确的批准不再重复询问 |
| Plan | ADAPT | `plan.md` 容纳 HOW、研究依据、候选取舍、依赖和验证方案；无需为简单 Stage 强制生成 data-model/quickstart/contracts 全套文件 |
| Tasks | ADAPT | 保留稳定 ID、需求映射、具体路径与依赖；执行投影由 accepted task digest 生成，只有 controller 可发出 executable work |
| Analyze | ADOPT | 在执行前进行只读一致性与覆盖检查；把 finding 交给既有 review/decision，不新增状态机 |
| Implement command | ALREADY_COVERED | Workflow 的 provider dispatch、STANDARD/FRONTIER 路由、执行证据和单一 owner 已承担执行职责；借鉴任务语法，不再安装第二个执行 owner |
| Converge gap inventory | ADAPT | 将 missing/partial/contradicts/unrequested 对应到 spec/plan/task 引用；形成待审任务提案，不能直接新增 iteration 或扩大 scope |
| Converge as Stage closure | REJECT | 代码/文档符合性只是技术证据；不能替代真实 GPT review、Human gate、integration receipt、post-integration verification |
| Flow-forward history | ADOPT | 已完成 Stage artifact 保留；需求变化通过 successor Stage 与明确链接继续前进 |
| Living Spec 的派生关系 | ADAPT | 当前工作中的 plan/task 可从已批准 spec 重建，但 accepted 版本与 rationale 保留；不把已接受的历史任务和计划当垃圾 |
| Living Spec 全量丢弃旧 plan/tasks | REJECT | 与本项目明确要求的审计、恢复和 Flow-forward 历史保留冲突 |
| Flow-back 任意 artifact 可定义最终意图 | REJECT | 实现发现可提出变更，但不能让 tasks 或代码悄悄与 approved intent 同权；变更须进入既有决策边界 |
| Native Spec Kit hooks 自动驱动生命周期 | REJECT | 不让模板 hook 绕过 controller 或创建第二条 decision/binding 路径 |
| Templates / optional presets | ADAPT | 先使用可追溯的精简模板原型；后续可做 preset。无需引入整个 extensions/bundles 生态作为 bootstrap 前提 |
| Immutable observations / versioned assessments / recovery | ALREADY_COVERED | Workflow V2 保持原协议，不从 Spec Kit 推导或替换这些机器语义 |

Converge 的重要限制：它看“当前实现是否满足当前意图”，不是 Git diff 或历史分析器；
所以它不能证明某次 provider 只修改了允许路径，也不能证明外部 integration 已落地。
这些证明继续来自 Workflow 的 observation、manifest 和 receipt。

## Authority 与可重建性

| Artifact | 谁定义/接受 | 权威范围 | 可否重建 | 长期 Git 策略 |
|---|---|---|---|---|
| constitution | Human 项目治理 | 长期原则与约束 | 不从 runtime 反推 | 版本化保留 |
| spec | Human intent，经既有批准绑定 digest | WHAT / WHY / 验收范围 | 可渲染，不可凭任务完成反向改需求 | accepted 版本保留 |
| plan | GPT planning + 已有设计批准边界 | HOW、选择与理由 | 未接受草稿可重建；accepted rationale 不丢 | accepted 版本保留 |
| tasks | accepted spec/plan 的工作分解 | 建议的 executable work 内容 | 草稿可重建；已执行任务 ID 不重用 | accepted/final 列表保留 |
| Stage contract / commands | 既有 canonical controller path | 实际 scope、budget、status、执行与 gate | 不由手写 Markdown 重建机器状态 | durable journal 与发布证据保留 |
| observations / assessments | Provider / validator，controller 收录 | 发生了什么 / 这个版本的评估是什么 | 不可覆盖 observation；新评估有新版本 | 恢复所需记录和重要证据保留 |
| CURRENT/PROJECT_HANDOFF | 只读投影器 | 当前导航，不决定 lifecycle | 可从 journal 与 artifact 索引重建 | 可选；不无限追加 |
| CLOSEOUT | canonical receipts 的 Human 摘要 | 解释结果、遗留项及证据链接 | 可重新渲染；已发布快照保留 | 保留 final；没有 CLOSED 不宣称已关闭 |
| upload/context presentation copy | pack builder | 展示或传输，无独立权威 | authority 与输入版本齐备才可重建 | 默认不进 Git |

“Derived”与“可删除”不是同义词。一个 accepted plan 虽然起源于 spec，仍包含
技术选择的历史价值；一个报告能重渲染，也可能是某次 review 实际见到的必要证据。
按引用与恢复依赖决定保留，不能只按扩展名或目录名决定清理。

## 本阶段与下一阶段

本阶段只交付采纳决策、ownership 映射、目标结构和 cleanup dry-run。
未运行 `specify init`，未写入 `.specify/`，未修改旧 Stage 文档名称。
建议先迁移指导文档，再隔离 project/runtime artifact，验证可移植性后才批准具体清理。
详细边界和阶段链见 `V2_PRODUCT_REPOSITORY_ARCHITECTURE.md`。
