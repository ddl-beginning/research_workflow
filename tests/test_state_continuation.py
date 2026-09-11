"""Process E2E tests. Reviewer is an explicit test double, never real GPT evidence."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from src.stage_controller import StageController
from src.state_continuation import derive_next, run_bounded, ContinuationError
from src.delivery_integration import integrate_delivery


def contract(root):
    return {"schema_version": "stage_contract.v1", "status": "PLANNED",
            "project_id": "process-e2e-fixture", "repository_root": str(root),
            "stage_id": "s", "stage_name": "S", "project_goal": "g", "stage_goal": "goal",
            "user_visible_goal": "u", "acceptance_description": "a",
            "allowed_paths": [".research"], "protected_paths": [".git"],
            "inputs": [], "required_checks": ["x"], "review_artifact_requirements": ["r"],
            "max_iterations": 2, "retry_budget": 2,
            "baseline": {"requirement_digest": "req", "design_digest": "design"}}


def setup(root):
    state = root / ".research/state.json"
    state.parent.mkdir(parents=True)
    c = StageController(contract(root), state_path=state)
    c.start_stage()
    (state.parent / "candidate.txt").write_text("fixture candidate", encoding="utf-8")
    return c


def complete_execution(c, request=None):
    request = request or c.request_executor()["request"]
    c.record_executor_result({"stage_id": "s", "iteration_index": request["iteration_index"],
        "status": "SUCCEEDED", "stage_ready": False, "required_checks": {"x": "PASS"},
        "review_artifacts": [{"type": "r", "uri": ".research/candidate.txt"}]},
        request_id=request["request_id"])


def worker(root, mode):
    state = root / ".research/state.json"
    c = StageController.from_state(state)
    def dispatch(route):
        fresh = StageController.from_state(state)
        assert fresh.state["revision"] == route["expected_revision"]
        log = state.parent / "dispatch.jsonl"
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), **route}) + "\n")
        if mode == "hold":
            (state.parent / "holding").write_text("locked")
            import time
            time.sleep(30)
            return {}
        if mode == "noop":
            return {}
        if route["capability"] == "stage.execution":
            if "request_id" in route:
                request = next(r for r in fresh.show_stage()["executor_requests"]
                               if r["request_id"] == route["request_id"])
            else:
                request = fresh.request_executor()["request"]
            if mode == "crash_request":
                os._exit(19)
            complete_execution(fresh, request)
            if mode == "gate_after":
                fresh.apply_decision("HUMAN_GATE", rationale="fixture semantic choice")
        elif route["capability"] == "review.technical":
            # Adapter double reconciles existing controller request and durable
            # provider receipt. It never pretends this is a real GPT consultation.
            binding = fresh.show_stage()["stage_result_binding"]["result_digest"]
            existing = [r for r in fresh.show_stage()["consultation_requests"]
                        if r["evidence_digest"] == binding]
            request = existing[-1] if existing else fresh.request_consultation(
                mode="FRESH", evidence_digest=binding)["request"]
            if existing:
                assert request["request_id"] in route["consultation_request_ids"]
            if mode == "crash_review_request":
                os._exit(21)
            review_file = state.parent / "test-double-review.json"
            if review_file.exists():
                review = json.loads(review_file.read_text())
            else:
                with (state.parent / "test-provider-invocations.jsonl").open("a") as f:
                    f.write(json.dumps({"request_id": request["request_id"], "pid": os.getpid()})+"\n")
                review = {"kind": "TEST_DOUBLE_NOT_GPT", "result_digest": binding,
                          "request_id": request["request_id"], "decision": "STAGE_READY"}
                review_file.write_text(json.dumps(review))
            assert review["result_digest"] == binding
            assert review["request_id"] == request["request_id"]
            if mode == "crash_review_receipt":
                os._exit(23)
            fresh.mark_stage_ready()
        elif route["capability"] == "integration":
            plan = {"stage_id": "s", "authoritative_integration_target": ".research/delivered.txt",
                "delivery_artifact_path": ".research/receipt.json",
                "allowed_paths": [".research"], "protected_paths": [".git"],
                "verification": {"command": [sys.executable, "-c",
                    "from pathlib import Path; assert Path('.research/delivered.txt').read_text() == 'fixture candidate'"]}}
            source = state.parent / "candidate.txt"
            integrate_delivery(fresh, stage_definition=plan,
                stage_contract=fresh.show_stage()["contract"], integration_artifact=plan,
                reviewed_result=fresh.show_stage()["latest_result"],
                review_receipt={"status": "complete", "stage_id": "s",
                    "consultation_id": "TEST-DOUBLE-NOT-GPT", "workflow_decision": "STAGE_READY",
                    "requirement_digest": "req", "design_digest": "design"},
                source_artifact_path=source,
                source_artifact_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
                controller_state=fresh.show_stage())
        if mode == "crash":
            os._exit(17)  # Lose ALL runner memory after controller commit, before callback return.
        return {"status": "COMPLETED"}
    trace = run_bounded(c, stage_id="s", dispatch=dispatch, max_steps=4)
    print(json.dumps(trace))


def child(root, mode):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), str(root), mode],
        text=True, capture_output=True, timeout=20)


def dispatches(root):
    p = root / ".research/dispatch.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_process_loss_reloads_disk_and_never_replays_committed_transitions(tmp_path):
    setup(tmp_path)
    statuses = []
    for expected in ["ACTIVE", "STAGE_READY", "APPROVED"]:
        result = child(tmp_path, "crash")
        assert result.returncode == 17, result.stderr
        statuses.append(StageController.from_state(tmp_path / ".research/state.json").show_stage("s")["status"])
        assert statuses[-1] == expected
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["route"]["position"] == "closed"
    calls = dispatches(tmp_path)
    assert [r["capability"] for r in calls] == ["stage.execution", "review.technical", "integration"]
    assert len({r["pid"] for r in calls}) == 3
    c = StageController.from_state(tmp_path / ".research/state.json")
    assert len(c.show_stage("s")["executor_requests"]) == 1
    assert c.state["active_stage_id"] is None
    receipt = json.loads((tmp_path / ".research/receipt.json").read_text())
    assert receipt["verification_result"]["passed"] is True


def test_crash_after_request_recovers_same_attempt(tmp_path):
    setup(tmp_path)
    assert child(tmp_path, "crash_request").returncode == 19
    state = tmp_path / ".research/state.json"
    before = StageController.from_state(state).show_stage("s")
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr
    calls = dispatches(tmp_path)
    assert calls[1]["position"] == "execution_recovery"
    assert calls[1]["request_id"] == before["executor_requests"][0]["request_id"]
    assert len(StageController.from_state(state).show_stage("s")["executor_requests"]) == 1


@pytest.mark.parametrize("mode,exit_code", [("crash_review_request", 21), ("crash_review_receipt", 23)])
def test_review_request_and_receipt_recovered_after_process_loss(tmp_path, mode, exit_code):
    c = setup(tmp_path)
    complete_execution(c)
    assert child(tmp_path, mode).returncode == exit_code
    request = StageController.from_state(c.state_path).show_stage("s")["consultation_requests"][0]
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr
    stage = StageController.from_state(c.state_path).show_stage("s")
    assert stage["status"] == "APPROVED"
    assert [r["request_id"] for r in stage["consultation_requests"]] == [request["request_id"]]
    assert len((tmp_path/".research/test-provider-invocations.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("completed", [False, True])
def test_persisted_human_gate_precedes_execution_and_review_zero_dispatch(tmp_path, completed):
    c = setup(tmp_path)
    if completed:
        complete_execution(c)
    stale = StageController.from_state(c.state_path)
    c.apply_decision("HUMAN_GATE", rationale="requirement decision")
    before = c.state_path.read_bytes()
    assert derive_next(stale)["position"] == "human_gate"
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["route"]["next_actor"] == "Human"
    assert dispatches(tmp_path) == []
    assert c.state_path.read_bytes() == before


def test_gate_committed_by_dispatch_precedes_next_dispatch(tmp_path):
    setup(tmp_path)
    result = child(tmp_path, "gate_after")
    assert result.returncode == 0, result.stderr
    assert [r["capability"] for r in dispatches(tmp_path)] == ["stage.execution"]
    assert json.loads(result.stdout)[-1]["route"]["position"] == "human_gate"


def test_stale_memory_does_not_override_disk_review_route(tmp_path):
    stale = setup(tmp_path)
    fresh = StageController.from_state(stale.state_path)
    complete_execution(fresh)
    assert derive_next(stale)["capability"] == "review.technical"


def test_real_controller_semantic_correction_restarts_only_next_iteration(tmp_path):
    stale = setup(tmp_path)
    complete_execution(stale)
    fresh = StageController.from_state(stale.state_path)
    fresh.record_technical_review({"decision": "TECHNICAL_MODIFICATION_REQUIRED",
        "requires_next_iteration": True, "review_id": "TEST-DOUBLE-CONTINUE",
        "rationale": "test double requires behavior correction"})
    assert derive_next(stale)["position"] == "execution"
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr
    stage = StageController.from_state(stale.state_path).show_stage("s")
    assert stage["iteration_index"] == 2
    assert len(stage["executor_requests"]) == 2
    assert stage["executor_requests"][-1]["attempt_index"] == 1


def test_consultation_request_survives_restart_and_rejects_duplicate(tmp_path):
    c = setup(tmp_path)
    complete_execution(c)
    c.request_consultation(mode="FRESH", evidence_digest="same-evidence")
    # The persisted consultation is recovered by the review adapter; continuation
    # only routes to that existing adapter and cannot create another consultation.
    fresh = StageController.from_state(c.state_path)
    before = c.state_path.read_bytes()
    with pytest.raises(Exception, match="same|duplicate|already"):
        fresh.request_consultation(mode="FRESH", evidence_digest="same-evidence")
    assert c.state_path.read_bytes() == before
    assert derive_next(c)["capability"] == "review.technical"


def test_no_committed_progress_stops_before_duplicate_effect(tmp_path):
    setup(tmp_path)
    result = child(tmp_path, "noop")
    assert result.returncode != 0 and "NO_COMMITTED_PROGRESS" in result.stderr
    assert len(dispatches(tmp_path)) == 1


def test_runner_exclusion_and_process_death_release(tmp_path):
    setup(tmp_path)
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(tmp_path), "hold"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        import time
        deadline = time.monotonic() + 10
        while not (tmp_path / ".research/holding").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / ".research/holding").exists()
        result = child(tmp_path, "normal")
        assert result.returncode != 0 and "CONTINUATION_ALREADY_RUNNING" in result.stderr
        assert len(dispatches(tmp_path)) == 1
    finally:
        proc.kill()
        proc.communicate(timeout=5)
    result = child(tmp_path, "normal")
    assert result.returncode == 0, result.stderr


def test_bounded_steps_and_disk_authority_required(tmp_path):
    c = setup(tmp_path)
    trace = run_bounded(c, dispatch=lambda r: (complete_execution(StageController.from_state(c.state_path)) or {}),
                        max_steps=1)
    assert trace[-1]["stop_reason"] == "STEP_BUDGET"
    assert trace[-1]["route"]["capability"] == "review.technical"
    for invalid in [True, 0, 33]:
        with pytest.raises(ContinuationError):
            run_bounded(c, dispatch=lambda r: {}, max_steps=invalid)
    with pytest.raises(ContinuationError, match="DISK_AUTHORITY_REQUIRED"):
        derive_next(StageController.from_snapshot(c.snapshot()))


if __name__ == "__main__":
    worker(Path(sys.argv[1]), sys.argv[2])
