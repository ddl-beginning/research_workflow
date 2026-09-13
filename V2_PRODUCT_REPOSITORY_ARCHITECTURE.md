# Workflow V2 Product Repository Architecture V1

Stage: `spec-kit-adoption-and-repository-architecture-v1`。
本文件是待 Human 审核的目标架构，不是已完成的仓库迁移。
Lifecycle baseline：`9203147ec2db6f990500c1995cffc61fbba2d595`，保持冻结。

## 核心决定

Workflow Engine 交付可安装的代码、schema、可复用模板、测试和产品说明。
使用它的项目拥有 intent、计划、执行历史、审查与关闭证据。
机器拥有安装位置、认证、缓存和临时传输目录。
同一个文件只由一个角色维护；引用别处的 authority 不再复制一份并独立编辑。

采用 Spec Kit 的文档职责分离与 Flow-forward 历史，不采用它作为第二套
执行或 lifecycle authority。研究依据见 `V2_SPEC_KIT_ADOPTION_DECISION.md`，
完整文件去向见 `audit/` 下的 inventory 和 `REPOSITORY_CLEANUP_DRY_RUN.md`。

## 三个根目录

以下树是设计目标。标有“未来”的能力尚未由本阶段实现。

```text
<engine-install-or-checkout>/
  src/                       # 可安装代码；冻结 V2 reducer 是唯一状态权威
  schemas/                   # 接口与数据协议，包括冻结 workflow_v2 schema
  templates/                 # 通用 guidance 模板，无具体项目内容
  scripts/                   # install/init/doctor/MCP 等可复用入口
  tests/                     # 可重复测试，小型离线 fixtures
  examples/                  # 合成示例和无机器路径的配置示例
  docs/                      # 安装、运维、接口、开发说明
  skills/                    # 可安装 guidance；禁止绑定历史实验现场
  README.md
  AGENTS.md                  # 可选，贡献规则与入口；不塞项目历史
  pyproject.toml             # 未来 packaging/install dependency contract
  .gitignore

<project-root>/
  <project-source>/          # 项目原有业务目录，不由 Workflow 强制重命名
  .specify/
    memory/constitution.md   # 项目原则；版本化
  specs/
    <canonical-stage-id>/
      spec.md                # WHAT / WHY / 验收及非目标
      plan.md                # HOW / 取舍 / 验证；研究附录按需
      tasks.md               # 稳定任务 ID、依赖、路径和验收引用
      CLOSEOUT.md            # 最终结果摘要；controller 未 CLOSED 时只能是草稿
      evidence/              # 只含需长期共享的关键证据/acceptance bundle
  .workflow-v2/
    journal.json             # 现有 controller durable journal；不改格式/语义
    journal.json.lock        # 本地锁；不属于长期历史
  .research/
    PROJECT_BRIEF.json       # 当前 intake 的机器 authority；迁移前仍保持原职责
    planning/                # accepted 规划证据与输入绑定
    reviews/                 # GPT consultation 绑定及 journal assessment 引用索引
    integration/             # operation/integration/verification receipts
    runtime/                 # 大型本地恢复材料，按引用保留
  PROJECT_HANDOFF.md         # 可选的当前导航/恢复索引；生成、不无限追加

<machine-runtime-root>/
  config/runtime-composition.json  # 复用现有配置 schema/解析机制
  auth/                       # 正常 Codex/Browser 所有，不搬入项目
  cache/<engine-version>/
  workspaces/<workspace-id>/
    runs/<operation-id>/
      context/                # 有界可再生展示包
      upload/                 # 传输 staging
      scratch/                # extraction/stdout/debug 临时文件
```

`<machine-runtime-root>` 由明确配置或平台本地目录解析：例如 Windows 的
LocalAppData，或其他平台对应的 user state/cache 目录；不以盘符和旧实验路径
作为产品 authority。现阶段 Browser Bridge 仍在 workspace 的 `.consultations`
创建 receipt/staging；把 staging 真正隔离到机器目录是后续 adapter 工作，不冒称已完成。

Engine 自己也可以作为项目被 self-host。它的开发工作区拥有自己的上述 project
artifact，但发布包/干净产品 checkout 不包含历史验证运行树。需共享的 self-host
历史进入独立的 engine-development 项目历史归档；不等于删除历史 Git commits。

## Human-facing：五种稳定职责，一个可选入口

| 文档 | 回答的问题 | 修改责任与边界 |
|---|---|---|
| constitution.md | 这个项目长期遵守什么原则？ | Human 治理；变更留版本与理由，不重写旧批准 |
| spec.md | 要什么、为什么、何时算成功？ | Human intent，经现有批准边界接受其确切版本 |
| plan.md | 怎么做、为什么选这条路线？ | GPT/实施者提出，按既有设计边界接受；保留关键 rationale |
| tasks.md | 当前需要完成哪些可验证工作？ | 从 accepted spec/plan 拆解；勾选不能替代 observation/assessment |
| CLOSEOUT.md | 最后得到了什么、证据和遗留项在哪里？ | 从 canonical 状态及 receipts 生成摘要；无独立关闭权力 |
| PROJECT_HANDOFF.md（可选） | 当前该看哪里、如何 resume？ | DERIVED 导航；不存第二份需求、计划或 lifecycle state |

日常先看导航，再看当前 spec/plan/tasks；constitution 是项目级文档，无需每 Stage
重复一份。与当前用户列出的 AGENTS、两种 handoff、brief、blueprint、requirement、
design、Stage plan、execution handoff、Stage contract、closeout 等交错职责相比，
目标为五种人类文档职责，外加一个可再生入口。机器合同仍存在，但不要求 Human
逐个阅读 JSON 才能理解状态。这里比较的是职责/文档类型，不把缺席路径计为现有文件。

## Machine-facing 与 authority 链

```text
Human intent / constitution
   -> accepted content version + existing decision binding
   -> GPT plan / tasks proposal
   -> canonical Stage contract and executable command
   -> immutable Provider Observation
   -> versioned Stage Result Assessment
   -> GPT technical Decision + required Human Decision
   -> integration / verification receipts
   -> controller CLOSEOUT
```

所有 Stage 状态、attempt、iteration、dependency、execution owner、decision resolver、
assessment/binding、journal/retry/revalidation 均沿用冻结 V2。Markdown 文件不是 reducer。
GUIDANCE 改名、目录移动和模板解析不得在 journal 旁边引入另一个“当前状态”文件。

Authority 分为不同问题，不能简单排一个“所有东西以 spec 为准”的总顺序：

- **产品意图**：Human 批准的 spec 版本定义 WHAT/WHY。当前实现仍由既有
  PROJECT_BRIEF intake 接受；未来迁移必须绑定 spec digest，不能同时保留两份可独立编辑的需求。
- **设计与执行内容**：accepted plan/tasks 定义建议 HOW/work；机器合同约束可执行 scope。
  文档与合同不一致时停下纠正输入绑定，不能让后来生成的 tasks 扩大 allowed paths。
- **已经发生的事实**：observation/receipts 定义；新 Markdown 摘要不得覆盖它们。
- **评估和允许的下一步**：versioned assessment、Decision 和 controller projection 定义。
  实施者、模板、converge 和导航文件均无权自行改变。

机器仍维护 journal、Stage contract、observation、assessment、Decision receipt、GPT
consultation receipt、integration/verification receipt 和 immutable manifests。
接口合同不改；目标只是来源清楚、位置清楚、Human 展示更简单。

## CURRENT → TARGET：文档职责映射

本表是语义映射；具体文件 action 的互斥统计以 cleanup inventory 为准，不将本表再加总。
不存在的当前文件标记为“概念/代码引用”，不虚构它们已被迁移。

| Current path/artifact | Current role | Problem | Target owner | Target path/artifact | Action |
|---|---|---|---|---|---|
| Engine `src/`, `schemas/` | 实现与协议 | V1/V2 入口曾混淆；不是历史垃圾 | ENGINE | 同职责目录，明确版本入口 | KEEP |
| 可复用 scripts/tests/examples/skills | 工具、回归、示例 | 与一次性 Phase 验证混杂 | ENGINE | 同职责；逐文件识别实验 fixture | KEEP |
| `AGENTS.md`（本 candidate 根目录缺席） | agent 行为入口 | 易重复需求与手动状态 | ENGINE / PROJECT，按其所在仓库 | 简短治理与 canonical 入口链接 | KEEP |
| `PROJECT_HANDOFF.md`（概念/历史使用） | 历史与当前导航混合 | 无限追加、摘要漂移 | DERIVED | 项目根当前导航 | MERGE |
| `LOCAL_BRIDGE_HANDOFF.md`（README 引用但缺席） | 本机 Bridge 操作与实验交接 | clone 后悬空且机器专属 | ENGINE / HISTORY | docs/runtime.md + owning project 历史证据 | MERGE |
| `.research/PROJECT_BRIEF.json` | 当前 accepted intake | 与人工 Requirement/spec 可能双写 | PROJECT | 保留机器 intake；未来绑定 `spec.md` 版本 | KEEP |
| PROJECT_BRIEF 人工正文 | 目标/范围/验收 | 与 Requirement 重复 | PROJECT | `specs/<id>/spec.md` | MERGE |
| Requirement | WHAT/WHY | 与 Brief 重复 | PROJECT | `spec.md` 需求/非目标/验收 | MERGE |
| Blueprint | 跨 Stage 方向、候选与路线 | 与 Design/Plan 重叠 | PROJECT | 总体理由并入相应 `plan.md`，必要时项目级附录 | MERGE |
| Design / DESIGN_PACKAGE | 方案与接受材料 | 重复 How 和手工状态 | PROJECT / HISTORY | plan 正文；机器接受 envelope 留 evidence | MERGE |
| STAGE_PLAN 人工部分 | 工作边界与任务 | 与 Design/Handoff 重复 | PROJECT | `plan.md` + `tasks.md` | MERGE |
| Stage contract / STAGE_PLAN 机器绑定 | 身份、scope、budget | 容易被误当作应删除的 JSON | HISTORY | 原协议 journal / 项目 evidence | KEEP |
| Execution Handoff | 给 executor 的输入摘要 | 可再生且过多副本 | DERIVED | 从 accepted plan/tasks/contract 按需投影 | MERGE |
| `.workflow-v2/journal.json` | 当前 V2 authority | 不能作为别的项目 bootstrap 模板 | PROJECT / HISTORY | 项目自己的同一 journal | KEEP |
| review packets / `.consultations` receipts | 审查输入及实际咨询证明 | 传输与 durable evidence 混放 | HISTORY | 项目 evidence + 本地恢复记录 | MOVE |
| `.consultations/staging`, `.tmp`, cache | 传输、临时上下文、计算副本 | 生命周期不明确 | TEMP | machine-runtime runs/scratch | DELETE_CANDIDATE |
| `implementation_evidence/phase-*` | self-host/验证历史 | project 历史进入 engine 发布树 | HISTORY | `<owning-engine-development-project>/implementation_evidence/` 长期归档 | MOVE |
| 历史 candidate copies | 旧验证现场 | 重复不代表全部可丢 | HISTORY / UNKNOWN | 按 manifest 引用与独特证据逐项判定 | UNKNOWN |
| 当前 Stage 四份架构报告及 audit | 本项目产出 | 本轮暂时放根方便审阅 | PROJECT / HISTORY | 未来 `specs/<stage-id>/` 与 evidence | MOVE |
| CLOSEOUT | 结论、integration、遗留事项 | 常与手工 handoff 状态重复 | HISTORY | `specs/<id>/CLOSEOUT.md` | KEEP |

`PROJECT_HANDOFF` 决策：**SIMPLIFY 其职责，合并重复内容**；映射中的合法 action
使用 MERGE，不引入新 action 枚举。最终它只列当前 Stage、canonical 下一步、
当前 spec/plan/tasks、最近关键 receipt、阻塞及 resume 命令。历史通过链接查阅。

## Flow-forward 与 retention

根据 [官方 persistence 说明](https://github.com/github/spec-kit/blob/d848fb4e18f44640ad6b42e60a280551ee90cdce/docs/concepts/spec-persistence.md)，
Flow-forward 保存已完成 feature，变更通过新的 feature 继续。这里将其对应到已接受
Stage identity 和 existing predecessor/dependency 关系，不再维护另一个谱系数据库。

| 层 | 保留内容 | 默认策略 | 清理前提 |
|---|---|---|---|
| Git Long-term History | constitution；accepted/final spec、plan、tasks；closeout；关键 falsification；可发布合同/Decision/Integration/Verification evidence bundle | 项目长期保留；已完成 Stage 不覆盖；敏感内容不进 Git | 正常历史不按 TTL 删除；任何删改需新的明确决策 |
| Local Project Runtime History | 完整 durable journal、resume 所需 observations/assessments/receipts、咨询 metadata、仍在引用的大型输入 | 项目本地 gitignore；独立备份，不当 cache | 存在未决 operation/Decision/dependency 或恢复引用时禁止 GC |
| Machine-local Temporary Runtime | 可再生 context copies、upload staging、extract scratch、stdout/debug/cache | gitignore；按 operation 隔离、大小与时间上限 | 传输已 settled、durable evidence 已保留、无人持有且引用检查通过 |

Git bundle 是分享/审计 artifact，不替代活跃 journal。现阶段 journal 为完整历史文件，
不实现截断、compaction 或重放迁移；这些若涉及 lifecycle contract，需独立 review。
备份/迁移项目时，保留完整 journal 和必要本地 evidence；从 Git clone 恢复若只有
发布的摘要 bundle，应明确显示历史只读、不能冒称已有可运行的完整 state。

同一 Stage 执行中的 plan/tasks 修订保留 accepted revision；converge 新任务必须
引用具体 finding 与 spec/plan ID，经过现有 GPT/typed replan 决策才可执行。
已 CLOSED 的 Stage 内容不原地改成新需求；successor 明确列前驱与变化理由。

## TEMP GC 的确定性规则（设计，不执行）

1. 仅由当前运行记录或 manifest 标识的机器 TEMP 根接受 GC；不以 `.json`、
   `.tmp` 字样或文件年龄单独断言可删。历史目录和 UNKNOWN 不自动进入 GC。
2. 必须检查 operation 已 settled/proven-not-sent、没有未决 Human/GPT gate、
   没有 in-flight provider、没有 active lease；文件仍被恢复或评估引用则保留。
3. 已审查 packet 的必要字节不能只存在 upload 副本中。若无法从 durable 输入及
   manifest 再构建审查视图，该副本升级为 HISTORY，不做 TEMP GC。
4. 清理前 dry-run 固定 manifest、计数和字节数；执行重新核对 realpath、哈希、
   引用和 workspace identity，任何漂移转 UNKNOWN；不跟随 symlink/reparse point。
5. 默认提出 closeout 后 7 天回收 staging、30 天回收一般 cache 的策略候选，
   不是已经生效的定时器。大小上限是 backpressure/提示，不能强删必要证据。
6. UNKNOWN 默认 KEEP；可选择项目本地 quarantine，但必须仍能寻址并保留引用。
   本阶段无 move/delete/GC 命令执行。

## Portability 与 init/resume

本轮已实现的最小 bootstrap：复用现有 runtime-composition schema，通过明确
`lifecycle_version=v2` 选择 Product adapter；机器 config 通过现有 external config
override 提供 Bridge/CLI 设置；已有 `research_workflow_cli.py init` 只检查依赖与
初始化空 V2 journal。随后 public MCP `workflow_run` 实际调用 GPT 规划并由 V2
注册 Stage。它不复用旧 `.research`、Stage state 或 consultation。

目标 install flow：clone/install engine → 配置机器 Bridge/CLI → 正常 Codex/ChatGPT
登录检测 → 对明确项目根 init → 现有 intake 批准版本 → canonical planning →
run/resume。机器缺少配置时输出具体 dependency code；不猜旧目录，也不回退旧 runtime。

现阶段尚有明确缺口：旧 installer skill-source/打包、历史 README 固定路径、
project identity/path 绑定以及 Bridge staging 路径等仍需后续工作。跨机器的完整
执行验证不是本轮两个临时路径的 bootstrap PASS 所能证明；见 portability audit。
Resume 只从 V2 journal 投影下一步；旧 workflow-state 可只读展示，不参与 V2 routing。

## 最小后续 Stage 链

| Stage | 交付与依赖 | 验收边界 |
|---|---|---|
| guidance-artifact-migration-v1 | 在本次 Human 架构接受后，模板与一个代表性项目的 spec/plan/tasks 映射；保留旧 artifact 与批准绑定 | Human 只读导航可找到目标/计划/任务/证据；不存在双写 intent 或状态 |
| project-artifact-isolation-v1 | 指导职责明确后，增加路径 resolver、project/history ownership 和 machine temp 边界；先复制验证/引用重绑定，再按单独许可处理旧位置 | engine 与 project 相互独立；恢复和 receipt 仍有效；新运行不写旧根 |
| portable-product-validation-v1 | 前两者完成后，补全 packaging/installer，独立机器或受控隔离环境进行 clone/install/auth/init/run/crash-resume | 不依赖旧磁盘或历史实验场；真实 GPT/provider 与负面路径通过 |
| legacy-repository-cleanup-v1 | 最后执行，依赖验证通过及 Human 对具体清单批准 | exact approved manifest；UNKNOWN 留存；归档可查、GC 引用安全、engine 发布树干净 |

将 cleanup 放最后，先证明 owner 与迁移后的恢复有效，才处理原位置。这个顺序与
原始候选阶段链不同，是研究与 bootstrap 发现带来的最小风险调整。

## Architecture Quality Gate

| 目标 | 本设计的可审核证明 |
|---|---|
| 更简单 | 三个物理 owner 根；五种 Human 文档职责；不新增 lifecycle state |
| 更少正式文档 | Brief/Requirement 合为意图正文；Blueprint/Design/Stage Plan 合为 plan；handoff 只生成导航 |
| 机器 authority 清楚 | 已发生事实、评估、许可、状态各自仍由原 V2 类型/路径负责 |
| ownership 清楚 | engine 发布物与 self-host 项目历史分离；每文件 dry-run owner/action 可查 |
| 历史保留清楚 | completed Stage 不覆盖；accepted rationale 与关键反证长期保留 |
| 临时生命周期清楚 | settled + 引用安全 + 可重建 + lease 检查才能清理；UNKNOWN 默认保留 |
| 可换电脑 | 明确 external machine config/正常认证/init/resume；剩余 blockers 列出，不掩饰现有路径依赖 |

结论仍需真实 GPT Technical Review 和 Human Architecture Gate；本文件本身不能
将 Stage 设为 READY/CLOSED，也不授予下一阶段 destructive migration 权限。
