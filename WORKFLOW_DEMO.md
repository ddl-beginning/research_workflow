# 完整 fake workflow

下面是一条有限、可重放的 flow：user plan → stage baseline → planner decision → scoped fake-Codex task → result → planner decision → stage ready → accept/reject。示例对应 `fixtures/stage_contract.json`；代码不会启动任何外部进程。

## 1. User plan 与 stage contract

Planner 先把真实目标与当前假设分开，并交付一个有边界的 stage：

```json
{
  "plan_id": "plan-demo-001",
  "stage_id": "stage-geometry-001",
  "user_visible_goal": "Reduce the visible contour artifact",
  "hypothesis": "The artifact is caused by contour sampling",
  "abstraction_layer": "contour-sampling",
  "allowed_paths": ["src/demo"],
  "critic_mode": "NONE",
  "max_iterations": 5
}
```

完整的可校验合同见 `fixtures/stage_contract.json`。它还声明 falsifier、acceptance、stop rules、read-first refs 和 measurement commands。

## 2. Stage start 与 baseline 固化

```python
from src.supervisor import Supervisor

supervisor = Supervisor()
start = supervisor.start_stage(
    contract,
    baseline_metrics={"artifact_score": 1.0, "runtime_ms": 80},
    baseline_artifact_refs=["artifact://baseline/geometry-v1"],
)
assert start["baseline_frozen"] is True
baseline_digest = start["baseline"]["digest"]
```

这个 digest 在整个 stage 中保持不变。候选 measurements 只通过 `compare_to_baseline()` 比较，不会覆写 baseline。

## 3. 第一个 scoped task 与普通 decision

```python
task1 = supervisor.create_scoped_task()
result1 = {
    "schema_version": "codex_result.v1",
    "plan_id": task1["plan_id"], "stage_id": task1["stage_id"],
    "task_id": task1["task_id"], "iteration_index": 1,
    "status": "SUCCEEDED", "summary": "artifact improved",
    "changed_files": ["src/demo/sampling.txt"],
    "tests": [{"name": "scoped smoke", "status": "PASS"}],
    "measurements": {"artifact_score": 0.7, "runtime_ms": 82},
    "evidence_refs": ["artifact://result/geometry-v1"],
    "abstraction_layer": "contour-sampling",
    "scientific_result": "SUPPORTED",
    "stage_ready": False,
    "user_visible_failure": False,
    "human_gate_required": False
}
event1 = supervisor.submit_result(task1, result1)
assert event1["decision"]["decision"] == "CONTINUE"
assert event1["decision"]["silent"] is True
assert event1["notification"] is None
```

普通 iteration 只更新本地 state、comparison、decision history 和 fresh critic packet，不要求用户复制粘贴或确认。

## 4. 第二个结果达到 stage ready

```python
task2 = supervisor.create_scoped_task()
result2 = dict(result1)
result2.update({
    "task_id": task2["task_id"],
    "iteration_index": 2,
    "summary": "acceptance measurements pass",
    "measurements": {"artifact_score": 0.5, "runtime_ms": 84},
    "evidence_refs": ["artifact://result/geometry-v2"],
    "review_artifacts": [{"uri": "artifact://stage/geometry/review-2.png"}],
    "stage_ready": True,
})
event2 = supervisor.submit_result(task2, result2)
assert event2["decision"]["decision"] == "STAGE_READY"
assert supervisor.status == "STAGE_READY"
assert event2["notification"]["type"] == "STAGE_READY"
assert event2["notification"]["review_artifacts"]
```

此时 context pack 仍只含约九类资源，且 baseline digest 仍为 `baseline_digest`。

## 5A. Accept 分支

```python
review = supervisor.accept_stage(
    reviewer="local-human",
    rationale="artifact improved and runtime stayed within budget",
)
assert review["status"] == "ACCEPTED"
assert review["accepted_milestone"]["stage_id"] == "stage-geometry-001"
assert supervisor.state["accepted_milestone"]["accepted"] is True
```

## 5B. Reject 分支（与 5A 二选一）

如果人认为证据不够，则在 `STAGE_READY` 状态调用：

```python
review = supervisor.reject_stage(
    reviewer="local-human",
    rationale="need one more controlled comparison",
)
assert review["status"] == "ACTIVE"
assert review["accepted_milestone"] is None
assert supervisor.state["accepted_milestones"] == []
task3 = supervisor.create_scoped_task()  # 显式、有限的下一轮
```

reject 不会偷偷接受结果，也不会开启后台 loop。

## 6. Scenario B：架构检查与静默 REPLAN

如果两个连续结果仍在 `contour-sampling` abstraction layer 且没有用户可见改善，
第二次 decision 会触发 architecture check，并在同一个 stage 内静默 `REPLAN`：

```json
{
  "decision": "REPLAN",
  "silent": true,
  "architecture_reset": true,
  "architecture_check": {
    "triggered": true,
    "trigger": "two_same_abstraction_no_improvement",
    "route": "REPLAN",
    "legacy_route": "ARCHITECTURE_RESET",
    "same_abstraction_count": 2,
    "planner_decision": "REPLAN"
  }
}
```

架构检查不会修改 `stage_id` 或 `user_visible_goal`，也不会自动开启下一 stage；普通 `CONTINUE` 和架构 `REPLAN` 都不产生通知。

## 7. Scenario C/D：有限 challenger 与 Human Gate

重大 representation replacement 可通过 `start_representation_comparison()` 或结果中的
`representation_replacement=true` 表达。状态切换为 `comparison_mode=MAJOR_CHALLENGER`，
只产生同一 `representative_set` 上的 `primary` 与 `challenger`，上限为两个 lane：

```python
comparison = supervisor.start_representation_comparison(["rep-a", "rep-b"])
assert comparison["active_lanes"] == ["primary", "challenger"]
winner = supervisor.select_winner("challenger", actor="local-human")
assert winner["active_lanes"] == ["challenger"]
```

重大不确定性（例如 ResultReport 的 `major_uncertainty=true` 或
`scientific_result="AMBIGUOUS"`）才进入 `HUMAN_GATE`。提交后不能创建 task，必须显式调用
`resolve_human_gate(approved=...)` 由人选择恢复或阻断。

## 8. 可重放验证

运行：

```text
python -m unittest discover -s tests -v
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

`tests/test_scenarios.py` 的 Scenario A–E 分别覆盖 improvement→CONTINUE→STAGE_READY→accept milestone、
同 abstraction 无改善→architecture REPLAN、major challenger/winner、重大不确定性 Human Gate、
以及拒绝 STAGE_READY 后保持原 stage ACTIVE；安全 scope/block 负测独立保留。所有 ID/digest 都由
canonical JSON 计算，因此同一输入会产生同一结果。
