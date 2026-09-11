# Bootstrap 交接说明

> 状态基线：2026-09-05。本文记录 Bootstrap（Step 11–14）输入及其到 Step 15
> 的交接边界。Step 15 只在这些 canonical outputs 和 `bootstrap_state.json` 通过
> 验证后显式运行，并把新 Stage 保持在 `PLANNED`。

## 当前范围与批准绑定

- 目标是一个 disposable、确定性的 Python word-summary fixture（`sample.txt`、
  `word_summary.py`、`tests/`），并评估有界、可复核的复用路线。
- 范围到 Project-scoped Requirements/Context、bounded Asset Pack、Project
  Discovery 和 CODEX Feasibility/Blueprint 为止。Candidate 0 只读审计；外部候选
  只作 metadata-only 行为参考，不自动选为实现，也不上传整个 repository。
- 已批准且必须严格使用的 Project URL：
  `https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project`
  （`scope=project`）。不要替换成 chat URL、其他 Project 或裸主页。
- 约束：不修改真实业务仓库、不改变 disposable fixture 行为、不创建或启动 Stage。

主要代码与合同入口：`src/project_intake.py`、`src/project_context.py`、
`src/artifact_retention.py`、`src/asset_layer.py`、`src/project_discovery.py`、
`src/project_blueprint.py`；验收脚本在 `scripts/step11_acceptance.py` 至
`scripts/step14_acceptance.py`。先读 `README.md`、`QUICKSTART.md`、
`ARCHITECTURE.md` 再继续。

## 已完成能力与离线验证

- Step 11：canonical requirements brief、一次一问的 intake、显式 approve/cancel
  gate；不调用 GPT，`gpt_calls`/`external_calls` 为 0。
- Step 12：只接受已批准 brief 的 bounded、deterministic context；canonical
  retention manifest、敏感字段/越界/ transcript 防护和受限 ephemeral 清理。
- Asset layer：统一 `AVAILABLE_ASSETS.json`，Candidate 0 只读 profile，外部候选
  需本地验证，保持 bounded metadata-only。
- Step 13：一次 `fresh` Project-scoped Discovery；Anti-Tunnel 规则；一个 primary、
  最多四个 alternatives；`no_direct_match_found` 合法；报告原子写入，重复
  evidence digest 和失败后的快速重试应被阻断。成功语义 marker 为
  `GPT_PROJECT_DISCOVERY_PASS`。
- Step 14：一次独立 `fresh` Blueprint review；compact `CODEX_FEASIBILITY` 不带前
  一轮 recommendation/sunk-cost 路线；一个 primary、最多四个 alternatives；
  原子 Blueprint/manifest；结果应为 `PROJECT_BLUEPRINT_READY` →
  `READY_FOR_STAGE_PLANNING`，且不创建/启动 Stage。成功语义 marker 为
  `GPT_CODEX_BLUEPRINT_REVIEW_PASS`。

离线 gate 命令（均不得产生真实 ChatGPT 请求）：

```text
python scripts/step11_acceptance.py
python scripts/step12_acceptance.py
python scripts/step13_acceptance.py
python scripts/step14_acceptance.py
python -m unittest discover -s tests -q
```

本轮交接前的准确离线结果如下；所有真实 ChatGPT 请求计数均为 0：

- Bridge：在 `D:\work\research_tools\chatgpt_browser_bridge` 执行
  `npm test -- --runInBand`，109 tests passed；`npm run check` 通过。
- Supervisor：在本目录执行 `python -m unittest discover -s tests -q`，166 tests
  passed。
- Step 11：`CODEX_NATIVE_REQUIREMENTS_INTAKE_PASS`。
- Step 12：`PROJECT_CONTEXT_COMPACTION_PASS`。
- Step 13：`GPT_PROJECT_DISCOVERY_PASS`。
- Step 14：`GPT_CODEX_BLUEPRINT_REVIEW_PASS`。

四个 Step acceptance 输出均带有 `new_real_chatgpt_requests=0`。Bridge 的
attachment hydration 修复包含一次有界 active-composer reattach/retry；只有
readiness timeout 才允许重挂载，显式 upload error 或不可靠 DOM 仍然
fail-closed。

## 真实 E2E 的当前准确状态

真实 Bootstrap 已在一个 headed-browser invocation 中完成，fixture 为：

`D:\work\research_tools\research_supervisor_poc\.tmp\real-bootstrap-e2e-jue4zg21`

批准的 Project URL 严格为：

`https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project`

Discovery 与 Blueprint 均为独立的 `fresh` Project-scoped call，且两个
conversation identity distinct：

- Discovery receipt：
  `.consultations/CONSULT-20260905-083637-fd02c36d/receipt.json`；
  consultation 为 `CONSULT-20260905-083637-fd02c36d`，conversation 为
  `6a9bd4a8-ef74-83e8-a416-b4cbc9b7c918`。
- Blueprint receipt：
  `.consultations/CONSULT-20260905-083816-0f5511d9/receipt.json`；
  consultation 为 `CONSULT-20260905-083816-0f5511d9`，conversation 为
  `6a9bd519-71a0-83ee-8cd0-60c47fc48c7a`。

两份 receipt 均验证 `status=complete`、`request_count=1`、
`project_scope_requested=true`、`project_scope_verified=true`、
`conversation_validated=true`，并落在 nested Project conversation URL。Discovery
的 6 个附件和 Blueprint 的 7 个附件均为 `upload_status=ready`。

同一 fixture 中的 canonical outputs 与 digest 如下：

- `.research/discovery/DISCOVERY_REPORT.json`：marker
  `GPT_PROJECT_DISCOVERY_PASS`，report digest
  `9731c6484028b71642df23de5e461be44bbd83c842366dbb1bf6dc9387fffc47`。
- `.research/blueprint/PROJECT_BLUEPRINT.md`：blueprint digest
  `8f3f4eb27ea742b4b986ac2d3a741e23223f6596319141f54560eb0fc35b0ccb`。
- `.research/blueprint/BLUEPRINT_MANIFEST.json`：marker
  `GPT_CODEX_BLUEPRINT_REVIEW_PASS`，manifest digest
  `6543673453f8b75952af31caa604cda080e4163e7c0f742ddef7b6d52d9a63c5`。
- `.research/ARTIFACT_RETENTION_MANIFEST.json`：digest
  `6f3df78748711a61cd93268fa9e557a6b7cd16345c6ec6273935b377de88238d`。
- `.research/bootstrap_state.json`：status `BOOTSTRAP_COMPLETE`，state digest
  `8ce3e1c582ab76c80cf044c5d8aa9c95d4e5a07fe6b1491fa8e0e4e660fa80a4`；其
  `markers` 包含 `PROJECT_BOOTSTRAP_RESEARCH_PASS` 和
  `PROJECT_SCOPED_BOOTSTRAP_E2E_PASS`，`request_counts` 为
  `discovery=1`、`blueprint=1`，`conversation_ids_distinct=true`，
  `project_scope_verified=true`。

真实 invocation stdout 的最终 markers 为：

```text
PROJECT_BOOTSTRAP_RESEARCH_PASS
PROJECT_SCOPED_BOOTSTRAP_E2E_PASS
```

以上 PASS 由 receipt、canonical artifacts、manifest/digest、provenance、
Project binding 和 state 校验共同产生；不能由 receipt 的
`status=complete`、聊天 URL 或模型回复内容单独升级。

## 剩余缺口与下一步

1. Bootstrap 已交接给 Step 15；Step 15 可消费本轮 fixture 下的 canonical
   outputs 与 `bootstrap_state.json`，并继续执行其自身的显式 planning gate。
2. 本轮没有创建或启动 Stage；`bootstrap_state.json` 明确记录
   `stage_created=false`、`stage_started=false`，下一状态仅为
   `READY_FOR_STAGE_PLANNING`。
3. 若后续需要重新运行 Bootstrap，只能使用批准的 Project URL、bounded
   attachments、串行低频 browser 和每个边界一次 `fresh` request；必须先读取
   failure receipt，禁止快速、并发或证据导向的重复发送。

## 硬性禁止事项

- `READY_FOR_STAGE_PLANNING` 只是 Step 15 的输入就绪状态；它不会自动创建或启动
  Stage。Step 15 的规划命令是 `python scripts/stage_cli.py --repo PATH plan-stage`，
  其输出必须由现有 StageController 注册为 `PLANNED`。
- 禁止伪造、手改、补写或重命名 receipt、marker、digest、Blueprint 或最终 PASS；
  transport complete 不得冒充 semantic PASS。
- 禁止失败后快速重试、并发重试或为了“凑齐”证据重复发送；先读 failure receipt，
  定位代码/输入问题，按既定退避和一次性请求策略行动。
- 禁止上传整个仓库、泄露 credentials/transcript、修改真实业务源代码，或绕过
  approved Project binding。

## 可直接复制给新 Codex 的启动提示

```text
你正在继续 D:\work\research_tools\research_supervisor_poc 的 Bootstrap 交接。
请先完整阅读 D:\work\research_tools\research_supervisor_poc\BOOTSTRAP_HANDOFF.md，
再阅读 README.md、QUICKSTART.md、ARCHITECTURE.md 以及 src/project_discovery.py、
src/project_blueprint.py 和 scripts/step11_acceptance.py–step14_acceptance.py。
然后只读审计代码、tests/、本轮 fixture 和其 receipts，核对 approved Project URL
是否严格为
https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project；确认
本轮 Discovery/Blueprint receipts、canonical artifacts、manifest/digest、
provenance 与 `bootstrap_state.json` 的一致性。Bootstrap 已产生
`PROJECT_BOOTSTRAP_RESEARCH_PASS` 和 `PROJECT_SCOPED_BOOTSTRAP_E2E_PASS`，并已
交接给 Step 15。Step 15 规划必须消费本轮真实 Bootstrap canonical outputs，报告
可复核的命令、路径、digest、Stage ID 和 `PLANNED` 状态；Bootstrap 本身不创建或
启动 Stage，且必须保持 `stage_started=false`。
```
