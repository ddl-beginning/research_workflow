"""Canonical Human Summary -> Machine Details presentation.

This module is a read-only projection over durable workflow evidence.  It does
not decide lifecycle transitions, approve a Stage, or invent scientific
evidence.  The same builder is used by the V2 runtime, MCP responses, the
portable launcher, and the canonical HUMAN_REVIEW artifact.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


PRESENTATION_STATUSES = ("CONTINUE", "STAGE_READY", "HUMAN_GATE", "BLOCKED", "PROGRAM_COMPLETE")
VISUAL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".html", ".htm", ".pdf"})
MACHINE_DETAIL_KEYS = (
    "CURRENT_STAGE",
    "STAGE_ID",
    "SCENES_COMPLETED",
    "EARLIEST_REMAINING_FAILURE",
    "GPT_DECISION",
    "CONSULTATION_ID",
    "CODE_CHANGED",
    "TESTS",
    "COMMIT",
    "REVIEW_ARTIFACT",
    "NEXT_ACTION",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, *, limit: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.replace("\x00", "").split())
    return value[:limit] if value else None


def _texts(value: Any, *, limit: int = 5) -> list[str]:
    if isinstance(value, str):
        candidate = _text(value)
        return [candidate] if candidate else []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:limit]:
        candidate = _text(item)
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _first(mappings: Sequence[Mapping[str, Any]], *names: str) -> Any:
    for mapping in mappings:
        for name in names:
            if name in mapping and mapping[name] not in (None, "", [], {}):
                return mapping[name]
    return None


def _stage_sources(metadata: Mapping[str, Any], canonical_state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_stage = _mapping(metadata.get("stage"))
    canonical_stage = _mapping(canonical_state.get("stage"))
    sources = [raw_stage, canonical_stage]
    for source in (raw_stage, canonical_stage):
        for nested_name in ("contract", "plan", "stage_plan"):
            nested = _mapping(source.get(nested_name))
            if nested:
                sources.append(nested)
    sources.extend(
        [
            _mapping(metadata.get("assessment")),
            _mapping(metadata.get("review_decision")),
            _mapping(metadata.get("technical_review")),
            _mapping(metadata.get("human_review")),
            _mapping(canonical_state),
        ]
    )
    return [item for item in sources if item]


def _artifact_values(sources: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    candidates: list[Any] = [metadata.get("artifact_refs"), metadata.get("review_artifacts")]
    for source in sources:
        candidates.extend([source.get("artifact_refs"), source.get("review_artifacts"), source.get("artifacts")])
    for candidate in candidates:
        items = candidate if isinstance(candidate, (list, tuple)) else [candidate]
        for item in items:
            if isinstance(item, Mapping):
                item = item.get("path") or item.get("ref") or item.get("name")
            value = _text(item, limit=1000)
            if value and value not in values:
                values.append(value)
    return values[:12]


def _is_visual(value: str, *, artifact_root: Path | None = None) -> bool:
    path = Path(value)
    if path.suffix.casefold() in VISUAL_EXTENSIONS:
        return True
    if artifact_root is not None:
        try:
            candidate = path if path.is_absolute() else artifact_root / path
            return candidate.is_file() and candidate.suffix.casefold() in VISUAL_EXTENSIONS
        except (OSError, RuntimeError):
            return False
    return False


def _points(value: Any, *, limit: int = 250) -> list[tuple[float, float]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[tuple[float, float]] = []
    for item in value[:limit]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if x != x or y != y or abs(x) > 1e9 or abs(y) > 1e9:
            continue
        result.append((x, y))
    return result


def _write_minimal_visual(
    artifact_root: Path | None,
    *,
    measured: Sequence[tuple[float, float]],
    candidate: Sequence[tuple[float, float]],
) -> str | None:
    """Write one bounded SVG only when measured and candidate evidence exist."""

    if artifact_root is None or not measured or not candidate:
        return None
    root = artifact_root.resolve()
    target = root / ".research" / "REVIEW_VISUAL.svg"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    all_points = [*measured, *candidate]
    min_x = min(item[0] for item in all_points)
    max_x = max(item[0] for item in all_points)
    min_y = min(item[1] for item in all_points)
    max_y = max(item[1] for item in all_points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    def point(item: tuple[float, float]) -> str:
        x = 40 + ((item[0] - min_x) / span_x) * 720
        y = 40 + ((max_y - item[1]) / span_y) * 520
        return f"{x:.2f},{y:.2f}"

    measured_svg = " ".join(
        f'<circle cx="{point(item).split(",")[0]}" cy="{point(item).split(",")[1]}" r="3" />'
        for item in measured[:250]
    )
    candidate_svg = " ".join(point(item) for item in candidate[:250])
    content = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600" '
        'role="img" aria-label="Measured points and candidate geometry review">'
        '<rect width="800" height="600" fill="white"/>'
        '<g fill="#2563eb" stroke="none">' + measured_svg + "</g>"
        '<polyline points="' + candidate_svg + '" fill="none" stroke="#dc2626" stroke-width="3"/>'
        '<text x="40" y="580" font-family="sans-serif" font-size="16">blue=measured, red=candidate</text>'
        "</svg>\n"
    )
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".REVIEW_VISUAL-", suffix=".tmp", dir=str(target.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, target)
        temporary = None
    except OSError:
        return None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return ".research/REVIEW_VISUAL.svg"


def _presentation_status(metadata: Mapping[str, Any], sources: Sequence[Mapping[str, Any]], canonical: Mapping[str, Any]) -> str:
    explicit = _first([metadata, *sources], "presentation_status", "terminal_state", "terminal_status")
    status = _text(explicit, limit=64)
    if status and status.upper() in PRESENTATION_STATUSES:
        return status.upper()
    decision = _text(_first([metadata, *sources], "workflow_decision", "gpt_decision", "decision"), limit=64)
    normalized_decision = (decision or "").upper()
    stage_status = (_text(_first(sources, "status", "stage_status"), limit=64) or "").upper()
    next_action = (_text(canonical.get("next_action"), limit=64) or "").upper()
    if stage_status in {"READY", "STAGE_READY"} or normalized_decision == "STAGE_READY":
        return "STAGE_READY"
    if stage_status in {"CLOSED", "PROGRAM_COMPLETE"}:
        return "PROGRAM_COMPLETE"
    if stage_status == "BLOCKED" or normalized_decision == "BLOCKED":
        return "BLOCKED"
    pending = _first(sources, "pending_human_gate", "pending_decisions", "human_gate_required")
    if normalized_decision == "HUMAN_GATE" or pending:
        return "HUMAN_GATE"
    if next_action in {"WAIT", "BLOCKED"}:
        return "BLOCKED"
    return "CONTINUE"


def _human_text(value: Any, *, default: str) -> str:
    result = _text(value, limit=1200)
    return result or default


def build_human_presentation(
    metadata: Mapping[str, Any] | None = None,
    *,
    canonical_state: Mapping[str, Any] | None = None,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build the six-part human summary and stable machine detail projection."""

    metadata = metadata if isinstance(metadata, Mapping) else {}
    canonical = canonical_state if isinstance(canonical_state, Mapping) else {}
    sources = _stage_sources(metadata, canonical)
    status = _presentation_status(metadata, sources, canonical)
    artifacts = _artifact_values(sources, metadata)
    root = Path(artifact_root).expanduser().resolve() if artifact_root else None
    review_mode = (_text(_first([metadata, *sources], "human_review_mode", "review_mode"), limit=64) or "summary").lower()
    if review_mode not in {"none", "summary", "visual", "semantic_choice"}:
        review_mode = "summary"
    visual_required = review_mode == "visual"
    visual_present = any(_is_visual(item, artifact_root=root) for item in artifacts)
    measured = _points(_first([metadata, *sources], "measured_points", "raw_points", "selected_points"))
    candidate = _points(_first([metadata, *sources], "candidate_points", "prediction_points", "boundary_points"))
    recovery = None
    if visual_required and not visual_present:
        recovery = _write_minimal_visual(root, measured=measured, candidate=candidate)
        if recovery:
            artifacts.append(recovery)
            visual_present = True
    presentation_status = "OK"
    if visual_required and not visual_present:
        presentation_status = "HUMAN_REVIEW_PRESENTATION_INCOMPLETE"

    if status == "STAGE_READY":
        if presentation_status != "OK":
            conclusion = "还不能接受，需要先补齐可供判断的视觉结果图。"
        else:
            conclusion = "可以接受，当前阶段已经完成，正在等待必要的验收动作。"
    elif status == "HUMAN_GATE" and presentation_status != "OK":
        conclusion = "还不能进入人工验收，因为可视化审查材料尚未就绪。"
    elif status == "HUMAN_GATE":
        conclusion = "需要你作一个明确选择，Workflow 暂时不会替你决定。"
    elif status == "BLOCKED":
        blocker = _human_text(_first([metadata, *sources], "blocker_summary", "blocker", "failure_reason"), default="当前依赖或外部输入尚未满足")
        conclusion = f"暂时无法继续，因为{blocker}。"
    elif status == "PROGRAM_COMPLETE":
        conclusion = "项目已完成，当前证据和验收记录已闭合。"
    else:
        conclusion = "Workflow 正在自动推进，你现在不需要介入。"

    done: list[str] = []
    for value in (
        _first([metadata, *sources], "accepted_evidence", "completed_evidence", "scenes_completed"),
    ):
        done.extend(_texts(value, limit=5))
    latest = _mapping(_first([metadata, *sources], "latest_result", "result"))
    done.extend(_texts(latest.get("summary"), limit=2))
    checks = _mapping(_first([metadata, *sources], "checks"))
    for name, value in list(checks.items())[:5]:
        if value is True:
            done.append(f"{_text(name, limit=100) or '检查'}已通过")
    if not done:
        if status == "CONTINUE":
            done.append("已恢复当前 Stage 的 canonical journal，后续动作仍由 Workflow 自动推进。")
        else:
            done.append("已保留当前 Stage 的结构化证据与可审计状态。")
    done = list(dict.fromkeys(done))[:5]

    if visual_required:
        if visual_present:
            visual_text = _human_text(
                _first([metadata, *sources], "visual_focus", "review_instructions"),
                default="请查看绑定的视觉 artifact，重点检查原始测量点与候选边界/曲面是否贴合，以及是否跨空洞乱连。",
            )
        else:
            visual_text = "当前没有可供视觉判断的 PNG、HTML 或 SVG；Workflow 会先执行 presentation recovery。"
    else:
        visual_text = _human_text(
            _first([metadata, *sources], "review_instructions", "evidence_summary"),
            default="本阶段以已绑定的结构化 evidence、检查结果和 Stage 状态为准。",
        )

    unproven = _texts(_first([metadata, *sources], "not_proven", "unproven", "known_limitations", "limitations"), limit=6)
    if not unproven:
        unproven = ["尚未声明额外限制；这不等于未列出的边界已经被证明。"]

    options = _first([metadata, *sources], "options", "allowed_choices")
    option_texts: list[str] = []
    if isinstance(options, (list, tuple)):
        for option in options[:5]:
            if isinstance(option, Mapping):
                label = _text(option.get("label") or option.get("id") or option.get("choice"), limit=160)
                meaning = _text(option.get("meaning") or option.get("tradeoff") or option.get("impact"), limit=320)
                option_texts.append(f"{label or '未命名选项'}：{meaning or '影响未声明'}")
            else:
                option_texts.extend(_texts(option, limit=1))
    if status == "HUMAN_GATE" and presentation_status != "OK":
        action = "你现在不需要选择算法路线；Workflow 会先自动生成或补齐最小 review visual。"
    elif status == "HUMAN_GATE":
        action = "请从以下选项中作出一个明确选择：" + "；".join(option_texts) if option_texts else "请查看机器详情中的 decision envelope，并提供一个明确选择。"
    elif status == "STAGE_READY" and presentation_status != "OK":
        action = "你现在不需要选择算法路线；请等待 Workflow 自动补齐最小 review visual。"
    elif status == "STAGE_READY" and review_mode in {"visual", "semantic_choice", "summary"}:
        action = "请按上面的说明查看绑定 evidence；如果这是明确的人工验收点，再确认是否可接受。"
    elif status in {"BLOCKED", "PROGRAM_COMPLETE"}:
        action = _human_text(_first([metadata, *sources], "human_action", "required_input", "user_action"), default="请根据阻塞原因补齐最小缺失输入。" if status == "BLOCKED" else "你现在不需要做任何事情。")
    else:
        action = "你现在不需要做任何事情，Workflow 会自动继续。"

    next_action = _text(canonical.get("next_action"), limit=160) or _text(_first([metadata, *sources], "next_action", "next_step"), limit=160)
    if status == "HUMAN_GATE":
        next_text = "等待你的明确选择后，Workflow 才会沿 decision envelope 继续。"
    elif status == "BLOCKED":
        next_text = "收到所需输入并通过现有 controller 检查后，Workflow 才会恢复。"
    elif status == "PROGRAM_COMPLETE":
        next_text = "不再自动创建新的 Stage。"
    elif next_action:
        next_text = f"下一步自动执行 {next_action}。"
    else:
        next_text = "如果没有 Human Gate，Workflow 会继续执行当前 policy 允许的下一步。"

    decision = _text(_first([metadata, *sources], "gpt_decision", "workflow_decision", "decision"), limit=160) or "未提供"
    consultation = _text(_first([metadata, *sources], "consultation_id", "review_id"), limit=200) or "未提供"
    changed = _texts(_first([metadata, *sources], "changed_files", "code_changed"), limit=12)
    tests = _first([metadata, *sources], "tests", "test_results")
    if isinstance(tests, list):
        test_text = "; ".join(
            f"{_text(item.get('name'), limit=100) or 'test'}={_text(item.get('status'), limit=40) or 'UNKNOWN'}"
            if isinstance(item, Mapping) else (_text(item, limit=120) or "UNKNOWN")
            for item in tests[:8]
        ) or "未提供"
    else:
        test_text = _text(tests, limit=500) or "未提供"
    machine_details = {
        "CURRENT_STAGE": _text(_first([metadata, *sources], "stage_name", "name"), limit=200) or "未命名 Stage",
        "STAGE_ID": _text(_first([metadata, *sources], "stage_id"), limit=200) or "未提供",
        "SCENES_COMPLETED": "；".join(_texts(_first([metadata, *sources], "scenes_completed"), limit=20)) or "未提供",
        "EARLIEST_REMAINING_FAILURE": _human_text(_first([metadata, *sources], "earliest_remaining_failure", "failure_code", "blocker"), default="未提供"),
        "GPT_DECISION": decision,
        "CONSULTATION_ID": consultation,
        "CODE_CHANGED": ", ".join(changed) or "未提供",
        "TESTS": test_text,
        "COMMIT": _text(_first([metadata, *sources], "commit", "commit_sha", "commit_id"), limit=200) or "未提供",
        "REVIEW_ARTIFACT": ", ".join(artifacts) or "未提供",
        "NEXT_ACTION": next_action or "未提供",
    }
    human_summary = {
        "status": status,
        "conclusion": conclusion,
        "what_was_done": done,
        "what_to_look_for": visual_text,
        "not_proven": unproven,
        "action_required": action,
        "next_step": next_text,
    }
    return {
        "schema_version": "workflow_human_presentation.v1",
        "presentation_status": presentation_status,
        "status": status,
        "review_mode": review_mode,
        "visual_artifact_required": visual_required,
        "visual_artifact_present": visual_present,
        "presentation_recovery": recovery,
        "human_summary": human_summary,
        "machine_details": machine_details,
    }


def render_human_presentation(presentation: Mapping[str, Any]) -> str:
    """Render the stable two-layer terminal form."""

    summary = _mapping(presentation.get("human_summary"))
    machine = _mapping(presentation.get("machine_details"))
    lines = [
        "HUMAN_SUMMARY",
        "",
        f"当前结论：{_human_text(summary.get('conclusion'), default='当前状态未提供。')}",
        "",
        "这轮做成了什么：",
    ]
    lines.extend(f"- {item}" for item in _texts(summary.get("what_was_done"), limit=5))
    lines.extend(["", "图上应该看到什么：", _human_text(summary.get("what_to_look_for"), default="本阶段以结构化 evidence 为准。"), "", "尚未证明/尚未实现："])
    lines.extend(f"- {item}" for item in _texts(summary.get("not_proven"), limit=6))
    lines.extend(["", "你现在需要做什么：", _human_text(summary.get("action_required"), default="你现在不需要做任何事情。"), "", "下一步：", _human_text(summary.get("next_step"), default="Workflow 会继续当前 policy 允许的动作。"), "", "MACHINE_DETAILS", ""])
    for key in MACHINE_DETAIL_KEYS:
        lines.append(f"{key}: {_human_text(machine.get(key), default='未提供')}")
    return "\n".join(lines)


__all__ = ["MACHINE_DETAIL_KEYS", "PRESENTATION_STATUSES", "build_human_presentation", "render_human_presentation"]
