"""Bounded Markdown project-plan ingestion for the portable Workflow product.

The two files under ``plan/`` are user input.  This module only normalizes
their meaning into the existing V2 ``StageController`` and two derived human
readable views.  It deliberately does not create a manifest, registry,
planner state machine, or second journal.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bridge_adapter import BridgeEnvelopeError, normalize_project_url
from .contracts import sha256_json
from .execution_profile import default_execution_profile
from .project_intake import BriefState, IntakeMode, ProjectIntakeError, ProjectRequirementsIntake
from .workflow_v2_contracts import derive_objective_fingerprint


PLAN_RELATIVE_PATH = Path("plan")
REQUIREMENTS_RELATIVE_PATH = PLAN_RELATIVE_PATH / "REQUIREMENTS.md"
STAGE_PLAN_RELATIVE_PATH = PLAN_RELATIVE_PATH / "STAGE_PLAN.md"
WORKFLOW_PLAN_RELATIVE_PATH = PLAN_RELATIVE_PATH / "WORKFLOW_PLAN.md"
CURRENT_STATE_RELATIVE_PATH = PLAN_RELATIVE_PATH / "CURRENT_STATE.md"
MAX_PLAN_SOURCE_BYTES = 512 * 1024
MAX_PLAN_STAGES = 64
MAX_PLAN_ITEMS = 32
MAX_PLAN_TEXT = 16_000


class ProjectPlanIngestionError(RuntimeError):
    """A bounded and user-actionable plan-ingestion failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


_REQUIREMENT_ALIASES = {
    "goal": {"goal", "projectgoal", "finalgoal", "projectobjective", "objective", "最终项目目标", "项目目标", "最终目标"},
    "scope": {"scope", "projectscope", "范围", "项目范围"},
    "non_goals": {"nongoals", "nongoal", "outofscope", "notinscope", "non_goals", "不做什么", "非目标", "不在范围"},
    "expected_output": {"output", "outputs", "expectedoutput", "finaloutput", "deliverables", "expectedoutputs", "最终输出", "预期输出", "交付物"},
    "input": {"input", "inputs", "data", "datasets", "resources", "输入", "数据", "数据集", "资源"},
    "success_criteria": {"acceptance", "acceptancecriteria", "success", "successcriteria", "quality", "precision", "验收", "验收标准", "最终验收标准", "质量", "精度", "质量精度要求"},
    "constraints": {"constraints", "businessconstraints", "businessrules", "约束", "业务约束", "限制"},
    "preferences": {"preferences", "偏好"},
    "problem_statement": {"problem", "problemstatement", "context", "背景", "问题", "问题描述"},
}

_STAGE_ALIASES = {
    "stage_id": {"stageid", "id", "阶段id", "阶段编号"},
    "goal": {"goal", "stagegoal", "objective", "stageobjective", "目标", "阶段目标"},
    "why": {"why", "whythisstageexists", "rationale", "reason", "为什么有这个阶段", "阶段原因"},
    "entry_conditions": {"entry", "entryconditions", "preconditions", "进入条件", "前置条件"},
    "inputs": {"input", "inputs", "stageinput", "输入", "阶段输入"},
    "datasets": {"data", "dataset", "datasets", "paths", "datapaths", "resources", "数据", "数据集", "路径", "数据路径", "资源"},
    "data_maturity": {"datamaturity", "maturity", "数据成熟度", "成熟度"},
    "dataset_role": {"role", "datasetrole", "datarole", "数据角色", "数据用途"},
    "gt_availability": {"gt", "groundtruth", "gtavailability", "groundtruthavailability", "真值", "gt可用性"},
    "reference_type": {"referencetype", "reference", "referencetype", "参考类型", "参考"},
    "tasks": {"task", "tasks", "work", "currentwork", "todo", "任务", "当前任务", "工作"},
    "expected_outputs": {"output", "outputs", "expectedoutput", "expectedoutputs", "deliverables", "预期输出", "输出", "交付物"},
    "human_visible_evidence": {"humanvisibleevidence", "humanvisible", "visibleevidence", "人类可见证据", "人工可见证据"},
    "machine_acceptance": {"machineacceptance", "automatedacceptance", "checks", "test", "tests", "验收方式", "机器验收", "自动验收"},
    "machine_primary": {"primary", "primarymachine", "primaryevaluation", "主要机器验收", "主要评估"},
    "machine_secondary": {"secondary", "secondarymachine", "secondaryevaluation", "次要机器验收", "诊断评估"},
    "human_acceptance": {"humanacceptance", "visualacceptance", "semanticacceptance", "人工验收", "视觉验收", "语义验收"},
    "human_gate_required": {"humangaterequired", "humangate", "requireshuman", "humanreview", "需要人工", "需要human", "人工门"},
    "dependencies": {"dependency", "dependencies", "depends", "依赖"},
    "replan_conditions": {"replan", "replanconditions", "representationfailure", "重新规划条件", "replan条件"},
    "stop_conditions": {"stop", "stopconditions", "停止条件", "阻断条件"},
    "pass_gate": {"passgate", "acceptancegate", "通过门槛", "通过条件"},
    "on_pass": {"onpass", "passaction", "通过后", "完成后"},
    "next_stage": {"nextstage", "next", "下一阶段", "下一stage"},
    "capabilities": {"capability", "capabilities", "requiredcapabilities", "能力", "所需能力"},
}

_FIELD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?P<label>[^:：]{1,96})(?:\*\*)?\s*[:：]\s*(?P<value>.+?)\s*$")
_EMPTY_FIELD_LABEL_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?P<label>[^:：]{1,96})(?:\*\*)?\s*[:：]\s*$")
_HEADING_RE = re.compile(r"^(?P<hash>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`<>()[\]{};,]+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_:/])/(?:[^\s\"'`<>()[\]{};,]+/)*[^\s\"'`<>()[\]{};,]+")
_RELATIVE_PATH_RE = re.compile(r"(?im)(?:path|路径|dataset|数据集|data|数据)\s*[:：]\s*([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_. -]+)+)")
_CHATGPT_PROJECT_LABEL_RE = re.compile(r"(?i)^\s*ChatGPT\s+Project\s+URL\s*[:：]\s*(?P<inline>.*?)\s*$")


def _root(project_root: str | os.PathLike[str]) -> Path:
    value = Path(project_root).expanduser().resolve()
    if not value.is_dir():
        raise ProjectPlanIngestionError("PROJECT_ROOT_INVALID", "project root is not a directory")
    return value


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.strip().lower())


def _field_key(value: str, aliases: Mapping[str, set[str]]) -> str | None:
    normalized = _normalize_label(value)
    if not normalized:
        return None
    for key, values in aliases.items():
        if normalized in {_normalize_label(item) for item in values}:
            return key
    return None


def _bounded_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_PLAN_SOURCE_BYTES:
            raise ProjectPlanIngestionError("PLAN_SOURCE_TOO_LARGE", f"planning input is larger than {MAX_PLAN_SOURCE_BYTES} bytes", details={"path": path.name})
        return path.read_text(encoding="utf-8")
    except ProjectPlanIngestionError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ProjectPlanIngestionError("PLAN_SOURCE_UNREADABLE", f"planning input could not be read: {path.name}", details={"path": path.name}) from exc


def detect_plan_sources(project_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Apply the same fixed discovery rule on every invocation."""

    root = _root(project_root)
    requirements = root / REQUIREMENTS_RELATIVE_PATH
    stage_plan = root / STAGE_PLAN_RELATIVE_PATH
    present = {
        "requirements": requirements.is_file(),
        "stage_plan": stage_plan.is_file(),
    }
    missing = [relative.as_posix() for relative, key in ((REQUIREMENTS_RELATIVE_PATH, "requirements"), (STAGE_PLAN_RELATIVE_PATH, "stage_plan")) if not present[key]]
    if not any(present.values()):
        status = "NO_PLAN"
    elif missing:
        status = "INCOMPLETE"
    else:
        status = "READY"
    diagnostic: dict[str, Any] | None = None
    if missing and (plan_dir := (root / PLAN_RELATIVE_PATH if (root / PLAN_RELATIVE_PATH).is_dir() else None)):
        candidates = sorted(
            (item for item in plan_dir.iterdir() if item.is_file()),
            key=lambda item: item.name.casefold(),
        )
        for required in (REQUIREMENTS_RELATIVE_PATH, STAGE_PLAN_RELATIVE_PATH):
            if required.as_posix() not in missing:
                continue
            expected_name = required.name
            variants = [
                item for item in candidates
                if item.name.casefold() != expected_name.casefold()
                and item.suffix.casefold() == ".md"
                and (
                    item.stem.casefold().startswith(required.stem.casefold())
                    or (required.name.casefold() == "requirements.md" and item.name.casefold() == "requirement.md")
                )
            ]
            numbered_requirements = [
                item for item in variants
                if required.name.casefold() == "requirements.md"
                and re.fullmatch(r"requirements \(\d+\)", item.stem, flags=re.IGNORECASE)
            ]
            if len(numbered_requirements) >= 2:
                status = "AMBIGUOUS"
                diagnostic = {
                    "code": "AMBIGUOUS_PLAN_SOURCE",
                    "expected": required.as_posix(),
                    "candidates": [str(item.relative_to(root)).replace("\\", "/") for item in numbered_requirements],
                    "message": f"Multiple possible sources found; create exactly {required.as_posix()} after choosing one authoritative source.",
                }
                break
            if variants and diagnostic is None:
                candidate_paths = [str(item.relative_to(root)).replace("\\", "/") for item in variants]
                diagnostic = {
                    "code": "MISSING_CANONICAL_PLAN_FILE",
                    "expected": required.as_posix(),
                    "candidates": candidate_paths,
                    "message": f"EXPECTED: {required.as_posix()}\nFOUND POSSIBLE MATCH: {candidate_paths[0]}\nRename the human source; Workflow will not rename it automatically.",
                }
    if diagnostic is not None:
        error_code = diagnostic["code"]
    elif status == "INCOMPLETE":
        error_code = "PLAN_INPUT_MISSING"
    else:
        error_code = None
    return {
        "status": status,
        "plan_folder": PLAN_RELATIVE_PATH.as_posix(),
        "requirements_path": REQUIREMENTS_RELATIVE_PATH.as_posix(),
        "stage_plan_path": STAGE_PLAN_RELATIVE_PATH.as_posix(),
        "present": present,
        "missing": missing,
        "user_source_files": [REQUIREMENTS_RELATIVE_PATH.as_posix(), STAGE_PLAN_RELATIVE_PATH.as_posix()],
        "generated_files": [WORKFLOW_PLAN_RELATIVE_PATH.as_posix(), CURRENT_STATE_RELATIVE_PATH.as_posix()],
        "error_code": error_code,
        "diagnostic": diagnostic,
        "message": diagnostic.get("message") if diagnostic else None,
    }


def _sections(text: str) -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append({"level": len(match.group("hash")), "title": match.group("title").strip(), "start": index, "body_start": index + 1, "end": len(lines)})
    for index, heading in enumerate(headings):
        for following in headings[index + 1:]:
            if following["level"] <= heading["level"]:
                heading["end"] = following["start"]
                break
    return [{**item, "body": lines[item["body_start"]:item["end"]]} for item in headings]


def _items(lines: Sequence[str]) -> list[str]:
    values: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line)
        line = line.replace("**", "").strip()
        if line:
            values.append(line[:MAX_PLAN_TEXT])
    # A prose section is one semantic item, while a list remains a list.
    return list(dict.fromkeys(values))[:MAX_PLAN_ITEMS]


def _collect_fields(text: str, *, aliases: Mapping[str, set[str]]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}

    def add(key: str | None, values: Sequence[str]) -> None:
        if key is None:
            return
        bucket = fields.setdefault(key, [])
        for value in values:
            if value and value not in bucket:
                bucket.append(value[:MAX_PLAN_TEXT])
        del bucket[MAX_PLAN_ITEMS:]

    for section in _sections(text):
        add(_field_key(str(section["title"]), aliases), _items(section["body"]))
    raw_lines = text.splitlines()
    for index, raw in enumerate(raw_lines):
        match = _FIELD_LABEL_RE.match(raw)
        if match:
            add(_field_key(match.group("label"), aliases), [match.group("value").strip()[:MAX_PLAN_TEXT]])
            continue
        empty = _EMPTY_FIELD_LABEL_RE.match(raw)
        if empty is None:
            continue
        key = _field_key(empty.group("label"), aliases)
        if key is None:
            continue
        continuation: list[str] = []
        for following in raw_lines[index + 1:]:
            if _HEADING_RE.match(following) or _FIELD_LABEL_RE.match(following) or _EMPTY_FIELD_LABEL_RE.match(following):
                break
            continuation.append(following)
        add(key, _items(continuation))
    return fields


def _first(fields: Mapping[str, Sequence[str]], key: str, default: str = "") -> str:
    values = fields.get(key) or []
    return str(values[0]).strip() if values else default


def _list_field(fields: Mapping[str, Sequence[str]], key: str) -> list[str]:
    return [str(item).strip() for item in (fields.get(key) or []) if str(item).strip()][:MAX_PLAN_ITEMS]


def _source_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_chatgpt_project_url(text: str) -> str | None:
    """Extract the one optional Project target from Workflow Binding only."""

    binding_sections = [
        section for section in _sections(text)
        if int(section["level"]) == 2
        and _normalize_label(str(section["title"])) == "workflowbinding"
    ]
    if not binding_sections:
        return None
    lines = [line for section in binding_sections for line in section["body"]]
    candidates: list[str] = []
    for index, line in enumerate(lines):
        match = _CHATGPT_PROJECT_LABEL_RE.match(line)
        if match is None:
            continue
        candidate = match.group("inline").strip()
        if not candidate and index + 1 < len(lines):
            candidate = lines[index + 1].strip()
        candidates.append(candidate.rstrip(".,;)"))
    if not candidates:
        return None
    if len(candidates) != 1 or not candidates[0]:
        raise ProjectPlanIngestionError("PROJECT_URL_INVALID", "ChatGPT Project URL must be declared exactly once", details={"field": "ChatGPT Project URL"})
    candidate = candidates[0]
    try:
        return normalize_project_url(candidate)
    except BridgeEnvelopeError as exc:
        raise ProjectPlanIngestionError(exc.code, "ChatGPT Project URL is invalid", details={"field": "ChatGPT Project URL"}) from exc


def _parse_requirements(text: str, project_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _collect_fields(text, aliases=_REQUIREMENT_ALIASES)
    paragraphs = _items(text)
    chatgpt_project_url = _parse_chatgpt_project_url(text)
    goal = _first(fields, "goal") or (paragraphs[0] if paragraphs else f"Execute the project plan for {project_name}.")
    output = _list_field(fields, "expected_output") or [goal]
    success = _list_field(fields, "success_criteria")
    if not success:
        success = ["The final output described in plan/REQUIREMENTS.md is produced and verified."]
    expected_input: Any = _list_field(fields, "input") or "The project workspace and inputs described in plan/STAGE_PLAN.md."
    brief = {
        "title": project_name,
        "problem_statement": _first(fields, "problem_statement") or f"Deliver the outcome described by the project plan for {project_name}.",
        "goal": goal,
        "desired_outcome": goal,
        "input": expected_input,
        "expected_output": output,
        "success_criteria": success,
        "acceptance_criteria": success,
        "constraints": _list_field(fields, "constraints"),
        "non_goals": _list_field(fields, "non_goals"),
        "scope": _list_field(fields, "scope"),
        "stakeholders": [],
        "preferences": _list_field(fields, "preferences"),
    }
    if chatgpt_project_url is not None:
        brief["chatgpt_project_url"] = chatgpt_project_url
        brief["chatgpt_project_binding"] = {
            "scope": "project",
            "url": chatgpt_project_url,
            "origin": "https://chatgpt.com",
            "url_digest": _source_digest(chatgpt_project_url),
        }
    structured = {
        "goal": goal,
        "scope": _list_field(fields, "scope"),
        "non_goals": _list_field(fields, "non_goals"),
        "expected_output": output,
        "quality_and_acceptance": success,
        "constraints": _list_field(fields, "constraints"),
        "raw_text": text[:MAX_PLAN_SOURCE_BYTES],
    }
    if chatgpt_project_url is not None:
        structured["chatgpt_project_url"] = chatgpt_project_url
    return brief, structured


def _stage_heading(title: str) -> tuple[str, str] | None:
    cleaned = re.sub(r"\*", "", title).strip()
    match = re.match(r"(?i)^stage\s+(?:id\s*[:：]\s*)?(?P<id>[A-Za-z][A-Za-z0-9_-]{0,63}|\d{1,3})(?:\s*[-:|—–]\s*(?P<title>.*))?$", cleaned)
    if match:
        return match.group("id"), (match.group("title") or match.group("id")).strip()
    match = re.match(r"^(?P<id>S\d{1,3}|[A-Za-z][A-Za-z0-9_-]{1,63})\s*[-:|—–]\s*(?P<title>.+)$", cleaned)
    if match:
        return match.group("id"), match.group("title").strip()
    if re.match(r"(?i)^S\d{1,3}$", cleaned):
        return cleaned, cleaned
    return None


def _canonical_stage_id(source_id: str, title: str, used: set[str]) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", source_id.strip()).strip("-").lower()
    if value.isdigit():
        value = "s" + value
    if not value:
        value = re.sub(r"[^A-Za-z0-9]+", "-", title.lower()).strip("-") or "stage"
    base = "stage-" + value
    candidate = base[:64]
    if len(candidate) < 8:
        candidate = (candidate + "-stage")[:64]
    suffix = 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = (base[:64 - len(tail)] + tail)
        suffix += 1
    used.add(candidate)
    return candidate


def _extract_paths(text: str) -> list[str]:
    values: list[str] = []
    for pattern in (_WINDOWS_PATH_RE, _POSIX_PATH_RE):
        values.extend(match.group(0) for match in pattern.finditer(text))
    values.extend(match.group(1) for match in _RELATIVE_PATH_RE.finditer(text))
    cleaned: list[str] = []
    for value in values:
        item = value.rstrip(".,;:)")
        if item and item not in cleaned and len(item) <= 512:
            cleaned.append(item)
    return cleaned[:MAX_PLAN_ITEMS]


def _truthy_gate(values: Sequence[str], text: str) -> bool:
    raw = " ".join(values).lower()
    if any(token in raw for token in ("no", "false", "否", "不需要", "无需")):
        return False
    return bool(values) and any(token in raw for token in ("yes", "true", "required", "需要", "是", "visual", "semantic", "人工")) or bool(re.search(r"(?i)human\s+gate|visual\s+gate|semantic\s+gate", text))


_NON_STAGE_WORK_WORDS = (
    "coding", "debug", "debugging", "parameter", "parameters", "helper", "function",
    "fix", "bug", "retry", "command", "run once", "adjust threshold", "修改参数",
    "调参数", "修复bug", "调试", "函数", "测试一次",
)


def _looks_like_stage(section: Mapping[str, Any], fields: Mapping[str, Sequence[str]], body: str) -> bool:
    """Accept a natural-language Stage only when it has Stage-shaped evidence."""

    title = str(section.get("title") or "").strip().lower()
    if any(token in title for token in _NON_STAGE_WORK_WORDS):
        return False
    signal_keys = {
        "goal", "why", "entry_conditions", "inputs", "datasets", "data_maturity",
        "tasks", "expected_outputs", "machine_acceptance", "machine_primary",
        "human_acceptance", "human_visible_evidence", "human_gate_required",
        "dependencies", "replan_conditions", "pass_gate", "next_stage",
    }
    if signal_keys.intersection(fields):
        return True
    return bool(re.search(
        r"(?i)\b(goal|objective|input|dataset|data|task|output|acceptance|gate|replan|next stage)\b|"
        r"阶段目标|阶段输入|数据|任务|输出|验收|重新规划|下一阶段",
        body,
    ))


def _inferred_maturity(text: str) -> str:
    lowered = text.lower()
    if "synthetic" in lowered and ("analytical" in lowered or "ground truth" in lowered or "gt" in lowered):
        return "Synthetic data with analytical GT"
    if "real" in lowered and ("frozen gt" in lowered or "ground truth" in lowered or "gt" in lowered):
        return "Real data with frozen GT"
    if "real" in lowered and ("competitor" in lowered or "human reference" in lowered):
        return "Real data with competitor/human reference"
    if "company" in lowered and ("without gt" in lowered or "no gt" in lowered or "without ground truth" in lowered):
        return "Real company data without GT"
    return "Not specified; validate data maturity at Stage entry."


def _dataset_specs(stage: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return bounded readable data bindings without creating a registry."""

    datasets = [str(item) for item in stage.get("datasets", []) if str(item).strip()]
    if not datasets:
        datasets = ["Data described by this Stage plan; resolve and validate before execution."]
    role = str(stage.get("dataset_role") or "Stage input")
    gt = str(stage.get("gt_availability") or "Not specified")
    reference = str(stage.get("reference_type") or "Not specified")
    maturity = str(stage.get("data_maturity") or "Not specified; validate data maturity at Stage entry.")
    return [
        {
            "dataset_name": item,
            "path": item if re.search(r"[\\/]", item) or re.match(r"^[A-Za-z]:", item) else "Not separately specified",
            "role": role,
            "gt_availability": gt,
            "reference_type": reference,
            "maturity": maturity,
        }
        for item in datasets[:MAX_PLAN_ITEMS]
    ]


def _parse_stages(text: str) -> list[dict[str, Any]]:
    headings = _sections(text)
    candidates: list[tuple[dict[str, Any], str, str]] = []
    for section in headings:
        heading_normalized = _normalize_label(str(section["title"]))
        if heading_normalized in {"stageplan", "plan", "stages", "overview", "阶段计划", "阶段"}:
            continue
        parsed = _stage_heading(str(section["title"]))
        heading_normalized = _normalize_label(str(section["title"]))
        body = "\n".join(str(item) for item in section.get("body", []))
        section_fields = _collect_fields(body, aliases=_STAGE_ALIASES)
        if parsed is None and section.get("level", 0) == 2 and heading_normalized not in {"stageplan", "plan", "stages", "overview", "阶段计划", "阶段"} and _field_key(str(section["title"]), _STAGE_ALIASES) is None and _looks_like_stage(section, section_fields, body):
            # A natural-language heading without an explicit ID is still a
            # stable source identity: its slug is deterministic and remains
            # unchanged when a later Stage is inserted before it.
            parsed = (re.sub(r"[^A-Za-z0-9]+", "-", str(section["title"]).lower()).strip("-") or "stage", str(section["title"]).strip())
        if parsed is None:
            continue
        source_id, title = parsed
        # Avoid interpreting field headings such as "Stage Goal" as Stages.
        if _field_key(str(section["title"]), _STAGE_ALIASES) is not None:
            continue
        candidates.append((section, source_id, title + ("\n" + body if body else "")))
    if not candidates:
        raise ProjectPlanIngestionError(
            "PLAN_STAGES_NOT_FOUND",
            "STAGE_PLAN.md contains no independently identifiable Stage; ordinary coding/debug steps are not Stages",
        )
    if len(candidates) > MAX_PLAN_STAGES:
        raise ProjectPlanIngestionError("PLAN_TOO_MANY_STAGES", f"STAGE_PLAN.md contains more than {MAX_PLAN_STAGES} stages")

    used: set[str] = set()
    parsed_stages: list[dict[str, Any]] = []
    for section, source_id, heading_and_body in candidates:
        body = "\n".join(str(item) for item in section.get("body", []))
        fields = _collect_fields(body, aliases=_STAGE_ALIASES)
        inline_id = _first(fields, "stage_id") if "stage_id" in fields else ""
        if inline_id:
            source_id = inline_id
        title = str(section.get("title") or source_id).strip()
        heading = _stage_heading(title)
        if heading:
            title = heading[1]
        canonical_id = _canonical_stage_id(source_id, title, used)
        goal = _first(fields, "goal") or title
        datasets = _list_field(fields, "datasets")
        datasets.extend(item for item in _extract_paths(heading_and_body) if item not in datasets)
        raw_lower = heading_and_body.lower()
        stage_owned = any(token in raw_lower for token in ("stage-owned", "stage owned", "由本阶段生成", "自动生成", "生成数据", "create the", "generate the", "produce the"))
        capabilities = _list_field(fields, "capabilities") or ["stage-plan-execution"]
        expected_outputs = _list_field(fields, "expected_outputs") or ["The outputs described by this Stage are present and accepted."]
        machine_acceptance = _list_field(fields, "machine_acceptance") or ["Run the machine checks and tests named in this Stage plan."]
        human_acceptance = _list_field(fields, "human_acceptance") or ["No additional Human acceptance unless Human Gate Required is YES."]
        gate_required = _truthy_gate(_list_field(fields, "human_gate_required"), heading_and_body)
        data_maturity = _first(fields, "data_maturity") or _inferred_maturity(heading_and_body)
        dependencies = _list_field(fields, "dependencies")
        entry_conditions = _list_field(fields, "entry_conditions") or [
            "The canonical predecessor is accepted and required Stage inputs are available or Stage-owned generation is legal."
        ]
        human_visible = _list_field(fields, "human_visible_evidence") or _list_field(fields, "human_acceptance") or [
            "Human-visible evidence is required only when this Stage declares a Human Gate."
        ]
        primary_machine = _list_field(fields, "machine_primary") or list(machine_acceptance)
        secondary_machine = _list_field(fields, "machine_secondary") or [
            "Runtime and diagnostic evidence is secondary and does not decide PASS."
        ]
        pass_gate = _list_field(fields, "pass_gate") or [
            "All Primary machine checks pass, required outputs exist, and no blocking failure remains."
        ]
        replan = _list_field(fields, "replan_conditions") or [
            "REPLAN only when evidence falsifies the technical route or the representation cannot satisfy the requirement; ordinary implementation failures stay within this Stage."
        ]
        stop = _list_field(fields, "stop_conditions") or [
            "Stop only for a validated Human Gate or a genuine external/private blocker with no legal automated next action."
        ]
        parsed_stages.append({
            "source_stage_id": source_id,
            "stage_id": canonical_id,
            "title": title,
            "stage_goal": goal,
            "why_this_stage_exists": _first(fields, "why"),
            "entry_conditions": entry_conditions,
            "inputs": _list_field(fields, "inputs"),
            "datasets": datasets[:MAX_PLAN_ITEMS],
            "dataset_role": _first(fields, "dataset_role"),
            "data_maturity": data_maturity,
            "gt_availability": _first(fields, "gt_availability"),
            "reference_type": _first(fields, "reference_type"),
            "dataset_specs": [],
            "tasks": _list_field(fields, "tasks") or [goal],
            "expected_outputs": expected_outputs,
            "human_visible_evidence": human_visible,
            "machine_acceptance": machine_acceptance,
            "machine_evaluation_primary": primary_machine,
            "machine_evaluation_secondary": secondary_machine,
            "human_acceptance": human_acceptance,
            "human_gate_required": gate_required,
            "pass_gate": pass_gate,
            "replan_conditions": replan,
            "stop_conditions": stop,
            "dependencies": dependencies,
            "next_stage_source_id": _first(fields, "next_stage"),
            "required_capabilities": capabilities,
            "stage_owned_data": stage_owned,
            "stage_self_contained_execution_readiness": True,
            "source_stage_digest": sha256_json({"source_id": source_id, "title": title, "body": heading_and_body[:MAX_PLAN_TEXT]}),
            "raw_text": heading_and_body[:MAX_PLAN_TEXT],
        })
    ids: dict[str, str] = {}
    for item in parsed_stages:
        source_id = str(item["source_stage_id"])
        ids[source_id] = item["stage_id"]
        ids[source_id.lower()] = item["stage_id"]
        if source_id.isdigit():
            ids["s" + source_id] = item["stage_id"]
    for index, item in enumerate(parsed_stages):
        explicit = item.get("next_stage_source_id")
        if explicit:
            explicit_value = str(explicit).strip()
            item["next_stage"] = None if explicit_value.lower() in {"none", "done", "end", "完成"} else ids.get(explicit_value, ids.get(explicit_value.lower(), explicit_value))
        elif index + 1 < len(parsed_stages):
            item["next_stage"] = parsed_stages[index + 1]["stage_id"]
        else:
            item["next_stage"] = None
        if not item["dependencies"]:
            item["dependencies"] = [parsed_stages[index - 1]["stage_id"]] if index else ["None; this is the first planned Stage."]
        else:
            normalized_dependencies: list[str] = []
            for dependency in item["dependencies"]:
                raw_dependency = str(dependency).strip()
                parts = [part.strip() for part in re.split(r"[,;]\s*", raw_dependency) if part.strip()]
                if not parts:
                    parts = [raw_dependency]
                for part in parts:
                    normalized_dependencies.append(ids.get(part, ids.get(part.lower(), part)))
            item["dependencies"] = list(dict.fromkeys(normalized_dependencies))
        if not item["why_this_stage_exists"]:
            if index:
                item["why_this_stage_exists"] = f"Adds the next independently verifiable milestone after {parsed_stages[index - 1]['stage_id']} without changing the earlier Stage's accepted evidence."
            else:
                item["why_this_stage_exists"] = "Establishes the first independently verifiable milestone for the project goal."
        item["dataset_specs"] = _dataset_specs(item)
        if index and item["dependencies"][0] == parsed_stages[index - 1]["stage_id"]:
            item["entry_conditions"] = [f"{parsed_stages[index - 1]['stage_id']} is accepted.", *item["entry_conditions"]]
    return parsed_stages


def load_project_plan(project_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and normalize the two user Markdown sources without GPT."""

    root = _root(project_root)
    discovery = detect_plan_sources(root)
    if discovery["status"] != "READY":
        if discovery["status"] in {"INCOMPLETE", "AMBIGUOUS"}:
            missing = ", ".join(discovery["missing"])
            raise ProjectPlanIngestionError(
                discovery.get("error_code") or "PLAN_INPUT_MISSING",
                discovery.get("message") or f"Missing required planning input: {missing}",
                details={"missing": discovery["missing"], "diagnostic": discovery.get("diagnostic")},
            )
        raise ProjectPlanIngestionError("PLAN_INPUT_NOT_FOUND", "no plan sources are present")
    requirements_path = root / REQUIREMENTS_RELATIVE_PATH
    stage_plan_path = root / STAGE_PLAN_RELATIVE_PATH
    requirements_text = _bounded_text(requirements_path)
    stage_plan_text = _bounded_text(stage_plan_path)
    brief, requirements = _parse_requirements(requirements_text, root.name)
    stages = _parse_stages(stage_plan_text)
    semantic_requirements = {
        key: value for key, value in requirements.items()
        if key not in {"raw_text", "chatgpt_project_url"}
    }
    return {
        "status": "READY",
        "root": str(root),
        "requirements_text": requirements_text,
        "stage_plan_text": stage_plan_text,
        "requirements_digest": _source_digest(requirements_text),
        "requirements_semantic_digest": sha256_json(semantic_requirements),
        "stage_plan_digest": _source_digest(stage_plan_text),
        "requirements": requirements,
        "brief": brief,
        "chatgpt_project_url": brief.get("chatgpt_project_url"),
        "chatgpt_target_mode": "PROJECT" if brief.get("chatgpt_project_url") else "DEFAULT",
        "chatgpt_target_url_digest": _source_digest(str(brief["chatgpt_project_url"])) if brief.get("chatgpt_project_url") else None,
        "stages": stages,
        "source_files": {
            "requirements": REQUIREMENTS_RELATIVE_PATH.as_posix(),
            "stage_plan": STAGE_PLAN_RELATIVE_PATH.as_posix(),
        },
    }


def ensure_plan_intake(project_root: str | os.PathLike[str], *, intake: ProjectRequirementsIntake | None = None) -> dict[str, Any]:
    """Create or complete the existing canonical Brief from plan sources."""

    root = _root(project_root)
    plan = load_project_plan(root)
    service = intake or ProjectRequirementsIntake(root)
    existing = service.state
    auto_approved = False
    if existing is None:
        service.initialize(
            mode=IntakeMode.USER_CONFIRMED_BRIEF,
            rough_requirement=str(plan["brief"]["goal"]),
            brief=plan["brief"],
            entrypoint="workflow-plan-ingestion",
        )
        existing = service.state
    elif existing.get("state") != BriefState.APPROVED.value:
        # Fill a pre-existing draft/approval boundary from the user's two
        # planning sources.  An already approved brief remains authoritative
        # and is never silently rewritten by a derived view.
        service.initialize(
            mode=IntakeMode.USER_CONFIRMED_BRIEF,
            rough_requirement=str(plan["brief"]["goal"]),
            brief=plan["brief"],
            entrypoint="workflow-plan-ingestion",
        )
        existing = service.state
    if existing is not None and existing.get("state") == BriefState.WAITING_USER_APPROVAL.value:
        service.approve(actor="workflow-plan-ingestion", rationale="approved user planning sources: plan/REQUIREMENTS.md and plan/STAGE_PLAN.md")
        auto_approved = True
    final = service.state
    if final is None or final.get("state") != BriefState.APPROVED.value:
        raise ProjectPlanIngestionError("PLAN_REQUIREMENTS_INCOMPLETE", "planning sources did not produce a complete canonical project brief")
    project_target_change = None
    target = plan.get("chatgpt_project_url")
    nested = final.get("brief") if isinstance(final.get("brief"), Mapping) else {}
    current = nested.get("chatgpt_project_url") if isinstance(nested, Mapping) else None
    binding = nested.get("chatgpt_project_binding") if isinstance(nested, Mapping) else None
    target_digest = _source_digest(target) if isinstance(target, str) and target else None
    binding_matches = (
        isinstance(target, str)
        and isinstance(current, str)
        and normalize_project_url(current) == target
        and isinstance(binding, Mapping)
        and binding.get("scope") == "project"
        and binding.get("url") == target
        and binding.get("origin") == "https://chatgpt.com"
        and binding.get("url_digest") == target_digest
    )
    history = final.get("project_config_changes") if isinstance(final.get("project_config_changes"), list) else []
    last_plan_binding = history[-1] if history and isinstance(history[-1], Mapping) else None
    recorded_target = any(
        isinstance(item, Mapping)
        and item.get("actor") == "workflow-plan-ingestion"
        and item.get("setting") == "chatgpt_project_url"
        and item.get("to_url_digest") == target_digest
        for item in history
    )
    should_clear_removed_target = (
        target is None
        and isinstance(current, str)
        and isinstance(last_plan_binding, Mapping)
        and last_plan_binding.get("actor") == "workflow-plan-ingestion"
        and last_plan_binding.get("source_requirements_digest") != plan["requirements_digest"]
    )
    if (not binding_matches or not recorded_target) and (isinstance(target, str) and target or should_clear_removed_target):
        try:
            bound = service.bind_chatgpt_project_target(
                target if isinstance(target, str) and target else None,
                source_requirements_digest=plan["requirements_digest"],
                source_path=REQUIREMENTS_RELATIVE_PATH.as_posix(),
                actor="workflow-plan-ingestion",
            )
        except ProjectIntakeError as exc:
            raise ProjectPlanIngestionError(exc.code, "canonical Project target binding failed") from exc
        final = bound["project_brief"]
        project_target_change = bound.get("project_target_change")
    return {
        "brief": final,
        "auto_approved": auto_approved,
        "plan": plan,
        "project_target_change": project_target_change,
    }


def _stage_object(plan_stage: Mapping[str, Any], *, project_id: str, workspace_id: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    profile = default_execution_profile()
    budget = copy.deepcopy(profile["budget_policy"])
    target_identity = "project/" + project_id
    capability = str((plan_stage.get("required_capabilities") or ["stage-plan-execution"])[0])
    objective = derive_objective_fingerprint(
        project_goal=str(plan["requirements"]["goal"]),
        acceptance_criteria=[str(item) for item in plan_stage.get("machine_acceptance", []) or [plan_stage.get("stage_goal")]],
        target_identity=target_identity,
        required_capability=capability,
    )
    baseline = "baseline-" + sha256_json({"requirements": plan.get("requirements_semantic_digest", plan["requirements_digest"]), "stage": plan_stage["source_stage_digest"]})
    return {
        "schema_version": "stage.v2",
        "stage_id": str(plan_stage.get("bound_stage_id") or plan_stage["stage_id"]),
        "workspace_id": workspace_id,
        "project_id": project_id,
        "objective_fingerprint": objective,
        "target_identity": target_identity,
        "required_capabilities": [str(item) for item in plan_stage.get("required_capabilities") or ["stage-plan-execution"]],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": baseline,
        "status": "PLANNED",
        "budgets": budget,
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def _read_previous_binding(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"requirements_digest": None, "stage_plan_digest": None, "stages": {}}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"requirements_digest": None, "stage_plan_digest": None, "stages": {}}
    requirements = re.search(r"^REQUIREMENTS_SHA256:\s*([0-9a-f]{64})\s*$", text, re.MULTILINE)
    semantic_requirements = re.search(r"^REQUIREMENTS_SEMANTIC_SHA256:\s*([0-9a-f]{64})\s*$", text, re.MULTILINE)
    stage_plan = re.search(r"^STAGE_PLAN_SHA256:\s*([0-9a-f]{64})\s*$", text, re.MULTILINE)
    stages: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        marker = re.match(r"^#{1,2}\s+Stage\s+([^ (—]+)", line)
        if marker:
            current = marker.group(1).strip()
        digest = re.match(r"^SOURCE_STAGE_SHA256:\s*([0-9a-f]{64})\s*$", line)
        if current and digest:
            stages[current] = digest.group(1)
    return {
        "requirements_digest": requirements.group(1) if requirements else None,
        "requirements_semantic_digest": semantic_requirements.group(1) if semantic_requirements else None,
        "stage_plan_digest": stage_plan.group(1) if stage_plan else None,
        "stages": stages,
    }


def _plan_change(plan: Mapping[str, Any], previous: Mapping[str, Any], statuses: Mapping[str, str], current_stage_id: str | None) -> dict[str, Any]:
    current_semantic = plan.get("requirements_semantic_digest", plan["requirements_digest"])
    previous_semantic = previous.get("requirements_semantic_digest") or previous.get("requirements_digest")
    requirement_changed = bool(previous_semantic and previous_semantic != current_semantic)
    target_only_changed = bool(
        previous.get("requirements_digest")
        and previous.get("requirements_digest") != plan["requirements_digest"]
        and previous_semantic == current_semantic
    )
    stage_plan_changed = bool(previous.get("stage_plan_digest") and previous.get("stage_plan_digest") != plan["stage_plan_digest"])
    previous_stages = previous.get("stages") if isinstance(previous.get("stages"), Mapping) else {}
    changed_stages = [item["stage_id"] for item in plan["stages"] if previous_stages.get(item["stage_id"]) not in (None, item["source_stage_digest"])]
    new_stages = [item["stage_id"] for item in plan["stages"] if item["stage_id"] not in previous_stages]
    affected = set(changed_stages)
    completed = [stage_id for stage_id in affected if statuses.get(stage_id) in {"CLOSED", "READY"}]
    current = [stage_id for stage_id in affected if stage_id == current_stage_id]
    if requirement_changed:
        impact = "FINAL_OBJECTIVE"
    elif target_only_changed:
        impact = "GPT_TARGET_ONLY"
    elif completed:
        impact = "COMPLETED_STAGE"
    elif current:
        impact = "CURRENT_STAGE"
    elif stage_plan_changed or new_stages or changed_stages:
        impact = "FUTURE_STAGES"
    else:
        impact = "NONE"
    return {
        "changed": bool(requirement_changed or target_only_changed or stage_plan_changed or changed_stages or new_stages),
        "requirements_changed": requirement_changed,
        "target_only_changed": target_only_changed,
        "stage_plan_changed": stage_plan_changed,
        "changed_stage_ids": changed_stages,
        "new_stage_ids": new_stages,
        "impact": impact,
        "technical_review_required": impact in {"FINAL_OBJECTIVE", "COMPLETED_STAGE", "CURRENT_STAGE"},
        "historical_records_preserved": True,
    }


def analyze_project_plan(plan: Mapping[str, Any], change: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Perform bounded, read-only structural plan analysis.

    This is deliberately an analysis result, not a planner or lifecycle
    authority.  Semantic product conflicts are surfaced as a review flag;
    this function never edits either user source or the canonical journal.
    """

    stages = [item for item in plan.get("stages", []) if isinstance(item, Mapping)]
    ids = [str(item.get("stage_id")) for item in stages]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    id_set = set(ids)
    dependency_edges: dict[str, set[str]] = {stage_id: set() for stage_id in ids}
    unknown_dependencies: list[str] = []
    for item in stages:
        stage_id = str(item.get("stage_id"))
        for dependency in item.get("dependencies", []) or []:
            candidate = str(dependency).strip()
            if candidate in id_set:
                dependency_edges[stage_id].add(candidate)
            elif candidate and not candidate.lower().startswith("none"):
                unknown_dependencies.append(f"{stage_id}->{candidate}")
        next_stage = item.get("next_stage")
        if isinstance(next_stage, str) and next_stage in id_set:
            dependency_edges[next_stage].add(stage_id)
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle = False

    def visit(stage_id: str) -> None:
        nonlocal cycle
        if stage_id in visiting:
            cycle = True
            return
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for predecessor in dependency_edges.get(stage_id, set()):
            visit(predecessor)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage_id in ids:
        visit(stage_id)
    missing_stage_fields: dict[str, list[str]] = {}
    required = (
        ("stage_goal", "goal"),
        ("entry_conditions", "entry_conditions"),
        ("dataset_specs", "data"),
        ("tasks", "tasks"),
        ("expected_outputs", "expected_outputs"),
        ("machine_evaluation_primary", "primary_machine_gate"),
        ("replan_conditions", "replan_conditions"),
        ("next_stage", "next_stage"),
    )
    for item in stages:
        missing = [label for key, label in required if key != "next_stage" and not item.get(key)]
        is_last_stage = bool(stages) and item is stages[-1]
        if item.get("next_stage") is None and not is_last_stage:
            missing.append("next_stage")
        if missing:
            missing_stage_fields[str(item.get("stage_id"))] = missing
    readiness = not duplicate_ids and not cycle and not unknown_dependencies and not missing_stage_fields and bool(stages)
    return {
        "status": "PASS" if readiness else "REVIEW_REQUIRED",
        "mode": "BOUNDED_STRUCTURAL_ANALYSIS",
        "requirements_coverage": "STRUCTURAL_COVERAGE_RECORDED",
        "orphan_stage_ids": [],
        "dependency_cycle": cycle,
        "unknown_dependencies": sorted(set(unknown_dependencies)),
        "duplicate_stage_ids": duplicate_ids,
        "missing_stage_fields": missing_stage_fields,
        "stage_merge_split_check": "PASS; ordinary engineering steps are kept inside Stage Tasks",
        "stage_maturity_order": "RECORDED_IN_SOURCE_ORDER",
        "stage_self_contained_execution_readiness": "PASS" if readiness else "FAIL",
        "technical_review_required": bool((change or {}).get("technical_review_required")),
        "product_semantic_conflict": bool((change or {}).get("impact") == "FINAL_OBJECTIVE"),
        "sources_mutated": False,
    }


def _render_workflow_plan(plan: Mapping[str, Any], change: Mapping[str, Any], *, current_stage_id: str | None = None, plan_analysis: Mapping[str, Any] | None = None) -> str:
    requirements = plan["requirements"]
    target_configured = bool(plan.get("chatgpt_project_url"))
    analysis = plan_analysis or {}

    def items(values: Sequence[Any], default: str) -> list[str]:
        selected = [str(value).strip() for value in values if str(value).strip()]
        return selected or [default]

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        "# Workflow Plan",
        "",
        "DERIVED EXECUTABLE PLAN VIEW",
        "",
        "This file is generated and maintained by Workflow.",
        "User planning sources remain authoritative for project meaning.",
        "Canonical runtime authority remains: StageController + journal + contracts.",
        "",
        f"REQUIREMENTS_SHA256: {plan['requirements_digest']}",
        f"STAGE_PLAN_SHA256: {plan['stage_plan_digest']}",
        f"REQUIREMENTS_SEMANTIC_SHA256: {plan.get('requirements_semantic_digest', plan['requirements_digest'])}",
        f"CHATGPT_TARGET_URL_SHA256: {plan.get('chatgpt_target_url_digest') or 'NONE'}",
        f"CANONICAL_CURRENT_STAGE: {current_stage_id or 'NONE'}",
        f"PLAN_CHANGE_IMPACT: {change.get('impact', 'NONE')}",
        f"TECHNICAL_REVIEW_REQUIRED: {'YES' if change.get('technical_review_required') else 'NO'}",
        f"GPT Review Workspace: {'BOUND_PROJECT' if target_configured else 'DEFAULT_BROWSER'}",
        f"GPT_REVIEW_MUST_USE_BOUND_PROJECT_TARGET: {'YES' if target_configured else 'NOT_APPLICABLE'}",
        f"PLAN_ANALYSIS: {analysis.get('status', 'PASS')}",
        f"STAGE_SELF_CONTAINED_EXECUTION_READINESS: {analysis.get('stage_self_contained_execution_readiness', 'PASS')}",
        "",
        "LEVEL 1 — PROJECT ROADMAP",
        "",
        "## Project Goal",
        "",
        str(requirements["goal"]),
        "",
        "## Scope",
        "",
        "### In Scope",
        "",
    ]
    lines.extend(f"- {item}" for item in items(requirements.get("scope", []), "Not explicitly provided; no additional scope is inferred."))
    lines.extend(["", "### Out of Scope", ""])
    lines.extend(f"- {item}" for item in items(requirements.get("non_goals", []), "Not explicitly provided; no additional non-goals are inferred."))
    lines.extend(["", "## Stage Roadmap", "", "| Stage ID | Goal | Depends On | Data | Main Evidence | Human Gate | Next |", "| -------- | ---- | ---------- | ---- | ------------- | ---------- | ---- |"])
    for stage in plan["stages"]:
        stage_id = stage.get("bound_stage_id") or stage["stage_id"]
        data = "; ".join(cell(spec.get("dataset_name")) for spec in stage.get("dataset_specs", []))
        evidence = "; ".join(cell(item) for item in items(stage.get("expected_outputs", []), "Required outputs exist and are accepted."))
        lines.append("| " + " | ".join((
            cell(stage_id),
            cell(stage.get("stage_goal")),
            cell(", ".join(stage.get("dependencies", []))),
            cell(data or "Stage data validated at entry"),
            cell(evidence),
            "YES" if stage.get("human_gate_required") else "NO",
            cell(stage.get("next_stage") or "DONE"),
        )) + " |")
    lines.extend([
        "",
        "## Requirements Interpretation",
        "",
        "The following is a bounded structural normalization; user semantics are preserved.",
        "",
    ])
    for key, title in (("expected_output", "Expected Final Outputs"), ("quality_and_acceptance", "Quality / Accuracy Requirements"), ("constraints", "Business Constraints")):
        lines.extend([f"### {title}", ""])
        lines.extend(f"- {item}" for item in items(requirements.get(key, []), "Not explicitly provided; no additional interpretation added."))
        lines.append("")
    lines.extend(["LEVEL 2 — STAGE CARDS", "", "## Stage Cards", ""])
    for stage in plan["stages"]:
        stage_id = stage.get("bound_stage_id") or stage["stage_id"]
        lines.extend([
            f"# Stage {stage_id} — {stage['title']}",
            "",
            f"SOURCE_STAGE_SHA256: {stage['source_stage_digest']}",
            f"PLAN_STAGE_ID: {stage['stage_id']}",
            f"BOUND_STAGE_ID: {stage_id}",
            f"STAGE_SELF_CONTAINED_EXECUTION_READINESS: {'PASS' if stage.get('stage_self_contained_execution_readiness') else 'FAIL'}",
            "",
            "## 1. Goal",
            "",
            f"Stage Goal: {stage['stage_goal']}",
            "",
            "## 2. Why This Stage Exists",
            "",
            str(stage.get("why_this_stage_exists") or "The source plan did not state a rationale; Workflow records only this bounded normalization."),
            "",
            "## 3. Entry Conditions",
            "",
        ])
        lines.extend(f"- {item}" for item in items(stage.get("entry_conditions", []), "Required predecessor and inputs are validated before execution."))
        lines.extend(["", "## 4. Inputs", "", "### Data", ""])
        for spec in stage.get("dataset_specs", []):
            lines.extend([
                f"- Dataset: {spec['dataset_name']}",
                f"  - Path: {spec['path']}",
                f"  - Role: {spec['role']}",
                f"  - Data maturity: {spec['maturity']}",
                f"  - GT availability: {spec['gt_availability']}",
                f"  - Reference type: {spec['reference_type']}",
            ])
        lines.extend(["", "### Prior Accepted Evidence", ""])
        lines.extend(f"- {item}" for item in items(stage.get("dependencies", []), "None; this is the first planned Stage."))
        lines.extend([
            "",
            "Data Ownership:",
            "- STAGE_OWNED_WORK may be generated automatically when the Stage owns the missing input." if stage.get("stage_owned_data") else "- Treat missing data as EXTERNAL_INPUT until runtime validation proves a legal automated owner.",
        ])
        lines.extend(["", "## 5. Work To Perform", ""])
        for index, task in enumerate(items(stage.get("tasks", []), stage["stage_goal"]), start=1):
            lines.append(f"- T{index:02d} — {task}")
        lines.extend(["", "## 6. Expected Outputs", "", "### Machine-readable", ""])
        lines.extend(f"- {item}" for item in items(stage.get("expected_outputs", []), "The Stage outputs are present and accepted."))
        lines.extend(["", "## 7. Machine Evaluation", "", "### Primary", ""])
        lines.extend(f"- {item}" for item in items(stage.get("machine_evaluation_primary", []), "Primary machine checks pass."))
        lines.extend(["", "### Secondary", ""])
        lines.extend(f"- {item}" for item in items(stage.get("machine_evaluation_secondary", []), "Secondary diagnostics do not decide PASS."))
        lines.extend(["", "## 8. Human-visible Evidence", ""])
        lines.extend(f"- {item}" for item in items(stage.get("human_visible_evidence", []), "Human does not need visual evidence unless the Human Gate is YES."))
        lines.extend([
            "",
            f"Human Gate Required: {'YES' if stage['human_gate_required'] else 'NO'}",
            "",
            "## 9. Pass Gate",
            "",
        ])
        lines.extend(f"- {item}" for item in items(stage.get("pass_gate", []), "Primary machine checks pass and required outputs exist."))
        lines.extend(["", "## 10. Replan Conditions", ""])
        lines.extend(f"- {item}" for item in items(stage.get("replan_conditions", []), "Replan only after a technical/representation failure is proven."))
        lines.extend(["", "## 11. Stop Conditions", ""])
        lines.extend(f"- {item}" for item in items(stage.get("stop_conditions", []), "Stop only for a validated Human Gate or genuine blocker."))
        lines.extend(["", "## 12. On PASS", "", f"Next Stage: {stage.get('next_stage') or 'DONE'}", "", "Workflow Action:", "- close current Stage through StageController", "- update CURRENT_STATE.md from canonical journal", "- load and validate the next planned Stage", "- resolve its data and continue automatically unless a valid Human Gate is pending", ""])
    if change.get("technical_review_required"):
        lines.extend(["## Plan Change Interpretation", "", "A source change touches the current, final, or accepted plan boundary. Workflow records a bounded technical/plan review requirement and does not rewrite journal history or user sources.", ""])
    return "\n".join(lines).rstrip() + "\n"


def _path_checks(root: Path, stage: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for raw in stage.get("datasets", []) or []:
        value = str(raw)
        candidate = Path(value).expanduser()
        resolved = candidate if candidate.is_absolute() else root / candidate
        exists = resolved.exists()
        checks.append({"path": value, "resolved_path": str(resolved.resolve()), "exists": exists, "classification": "EXISTS" if exists else "STAGE_OWNED_WORK" if stage.get("stage_owned_data") else "EXTERNAL_INPUT", "next_action": "USE" if exists else "GENERATE_STAGE_OWNED_INPUT" if stage.get("stage_owned_data") else "VALIDATE_EXTERNAL_INPUT"})
    return checks


def _render_current_state(root: Path, plan: Mapping[str, Any], projection: Mapping[str, Any], change: Mapping[str, Any], path_checks: Sequence[Mapping[str, Any]]) -> str:
    stage_view = projection.get("stage") if isinstance(projection.get("stage"), Mapping) else {}
    current_id = stage_view.get("stage_id")
    statuses = {str(key): str(value.get("status")) for key, value in (projection.get("journal_stages") or {}).items() if isinstance(value, Mapping)}
    if not statuses:
        for item in plan.get("stages", []):
            bound_id = str(item.get("bound_stage_id") or item.get("stage_id"))
            if bound_id == current_id:
                statuses[bound_id] = str(stage_view.get("status") or "PLANNED")
    def bound_id(item: Mapping[str, Any]) -> str:
        return str(item.get("bound_stage_id") or item.get("stage_id"))
    completed = [bound_id(item) for item in plan["stages"] if statuses.get(bound_id(item)) == "CLOSED"]
    accepted = [bound_id(item) for item in plan["stages"] if statuses.get(bound_id(item)) == "CLOSED"]
    remaining = [bound_id(item) for item in plan["stages"] if statuses.get(bound_id(item)) not in {"CLOSED", "STOPPED"}]
    selected = next((item for item in plan["stages"] if bound_id(item) == current_id), None)
    legal = projection.get("legal_next_action") if isinstance(projection.get("legal_next_action"), Mapping) else {}
    blocker = projection.get("gpt_decision") if projection.get("gpt_decision") in {"BLOCKED", "HUMAN_GATE"} else "None recorded by canonical controller."
    progress = f"status={stage_view.get('status', 'NOT_STARTED')}; iteration={stage_view.get('iteration_count', 0)}; attempts={stage_view.get('attempt_count', 0)}"
    if path_checks:
        missing = [item["path"] for item in path_checks if not item.get("exists")]
        if missing:
            progress += "; missing inputs=" + ", ".join(missing)
    lines = [
        "# Current State",
        "",
        "DERIVED HUMAN-READABLE SNAPSHOT",
        "",
        "This file is not lifecycle authority.",
        "",
        "Canonical runtime authority remains:",
        "StageController + journal + contracts.",
        "",
        "## Project",
        "",
        f"Goal: {plan['requirements']['goal']}",
        "",
        "## Current Stage",
        "",
        f"Stage ID: {current_id or 'NONE'}",
        f"Current Stage: {current_id or 'NONE'}",
        f"Stage Name: {selected.get('title') if selected else 'NONE'}",
        f"Status: {stage_view.get('status') or 'NOT_STARTED'}",
        f"Current Status: {stage_view.get('status') or 'NOT_STARTED'}",
        f"Current Objective: {selected.get('stage_goal') if selected else 'No active Stage objective.'}",
        "",
        "## Completed Stages",
        "",
        f"{', '.join(completed) if completed else 'None'}",
        f"Completed Stages: {', '.join(completed) if completed else 'None'}",
        f"Accepted Stages: {', '.join(accepted) if accepted else 'None'}",
        "",
        "## Current Work",
        "",
        f"Progress: {progress}",
        f"Current Progress: {progress}",
        f"Current Work: {', '.join(selected.get('tasks', [])) if selected else 'No active Stage work.'}",
        f"Current Blocker: {blocker}",
        f"Next Legal Action: {legal.get('action') or projection.get('next_action') or 'NONE'}",
        "",
        "## Remaining Stages",
        "",
        f"{', '.join(remaining) if remaining else 'None'}",
        "",
        "## Plan Status",
        "",
        "Source files: plan/REQUIREMENTS.md + plan/STAGE_PLAN.md",
        "Current Plan Source: plan/REQUIREMENTS.md + plan/STAGE_PLAN.md",
        f"Source change detected: {'YES' if change.get('changed') else 'NO'}",
        f"Plan Source Digests: requirements={plan['requirements_digest']}; requirements_semantic={plan.get('requirements_semantic_digest', plan['requirements_digest'])}; stage_plan={plan['stage_plan_digest']}",
        f"GPT Review Target: {'CONFIGURED' if plan.get('chatgpt_project_url') else 'DEFAULT'}",
        f"Plan Change: {change.get('impact', 'NONE')} (technical review required={'YES' if change.get('technical_review_required') else 'NO'})",
        "",
        "Journal wins over this snapshot if any displayed field conflicts with canonical state.",
    ]
    return "\n".join(lines) + "\n"


def _write_derived(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeError):
            pass
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".workflow-plan-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ProjectPlanIngestionError("PLAN_DERIVED_WRITE_FAILED", f"could not maintain {path.name}") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return True


def _align_existing_stage_ids(plan: dict[str, Any], state: Mapping[str, Any]) -> None:
    """Bind a plan-only view to one legacy canonical Stage without replaying it.

    A project may acquire plan sources after a Stage was registered by an
    earlier bounded workflow.  When there is exactly one unmatched canonical
    Stage, positional alignment is safe and deterministic; larger ambiguous
    graphs remain untouched and are left for the existing technical review
    path rather than guessed.
    """

    existing = state.get("stages") if isinstance(state.get("stages"), Mapping) else {}
    if not existing:
        return
    canonical_ids = {str(item.get("stage_id")) for item in plan.get("stages", [])}
    unmatched = [str(stage_id) for stage_id in existing if str(stage_id) not in canonical_ids]
    stages = plan.get("stages") if isinstance(plan.get("stages"), list) else []
    if len(unmatched) == 1 and stages and str(stages[0].get("stage_id")) not in existing:
        stages[0]["bound_stage_id"] = unmatched[0]
    elif len(unmatched) == len(stages) and len(stages) > 1:
        # Existing projects may have canonical IDs chosen before plan files
        # were added.  A same-card-count positional binding is the only
        # bounded inference made here; it preserves every existing status and
        # cannot claim completion from files alone.
        for item, existing_id in zip(stages, unmatched):
            item["bound_stage_id"] = existing_id
    for item in stages:
        item.setdefault("bound_stage_id", item.get("stage_id"))


def sync_project_plan(project_root: str | os.PathLike[str], *, controller: Any | None = None, intake: ProjectRequirementsIntake | None = None, auto_start: bool = True) -> dict[str, Any]:
    """Ingest sources, bind missing Stages, refresh views, and optionally start the next Stage."""

    root = _root(project_root)
    discovery = detect_plan_sources(root)
    if discovery["status"] == "NO_PLAN":
        return {"status": "LEGACY_COMPATIBLE", "plan_discovery": discovery, "human_intervention_count": 0}
    if discovery["status"] not in {"NO_PLAN", "READY"}:
        return {"status": "INCOMPLETE", "plan_discovery": discovery, "missing": discovery["missing"], "human_intervention_count": 0}
    prepared = ensure_plan_intake(root, intake=intake)
    plan = prepared["plan"]
    if controller is not None:
        controller.initialize()
        _align_existing_stage_ids(plan, controller.state)
    statuses: dict[str, str] = {}
    current_stage_id: str | None = None
    projection: dict[str, Any] = {}
    registered: list[str] = []
    started = False
    if controller is not None:
        existing_state = controller.state
        statuses = {str(key): str(value.get("status")) for key, value in existing_state.get("stages", {}).items() if isinstance(value, Mapping)}
        before_projection = controller.resume_projection()
        before_stage = before_projection.get("stage") if isinstance(before_projection.get("stage"), Mapping) else {}
        current_stage_id = before_stage.get("stage_id")
        for plan_stage in plan["stages"]:
            stage_id = str(plan_stage.get("bound_stage_id") or plan_stage["stage_id"])
            if stage_id not in existing_state.get("stages", {}):
                controller.register_stage(_stage_object(plan_stage, project_id=prepared["brief"]["project_id"], workspace_id=controller.workspace_id, plan=plan), command_id="command-plan-register-" + sha256_json({"stage_id": stage_id, "source": plan_stage["source_stage_digest"]})[:32])
                registered.append(stage_id)
        statuses = {str(key): str(value.get("status")) for key, value in controller.state.get("stages", {}).items() if isinstance(value, Mapping)}
        active = [stage_id for stage_id, status in statuses.items() if status in {"ACTIVE", "READY"}]
        if auto_start and not active:
            for plan_stage in plan["stages"]:
                stage_id = str(plan_stage.get("bound_stage_id") or plan_stage["stage_id"])
                if statuses.get(stage_id) != "PLANNED":
                    continue
                prior_ids = [str(item.get("bound_stage_id") or item["stage_id"]) for item in plan["stages"][:plan["stages"].index(plan_stage)]]
                if all(statuses.get(item) in {"CLOSED", "STOPPED", None} for item in prior_ids):
                    controller.start(stage_id, command_id="command-plan-start-" + sha256_json({"stage_id": stage_id})[:32])
                    started = True
                    current_stage_id = stage_id
                    break
        projection = controller.resume_projection()
        stage_view = projection.get("stage") if isinstance(projection.get("stage"), Mapping) else {}
        current_stage_id = stage_view.get("stage_id") or current_stage_id
        projection = {**projection, "journal_stages": controller.state.get("stages", {})}
    else:
        projection = {"stage": {}, "next_action": "START", "legal_next_action": {"action": "START"}}
    previous = _read_previous_binding(root / WORKFLOW_PLAN_RELATIVE_PATH)
    change = _plan_change(plan, previous, statuses, current_stage_id)
    plan_analysis = analyze_project_plan(plan, change)
    change = {**change, "plan_analysis": plan_analysis}
    workflow_plan_written = _write_derived(
        root / WORKFLOW_PLAN_RELATIVE_PATH,
        _render_workflow_plan(plan, change, current_stage_id=current_stage_id, plan_analysis=plan_analysis),
    )
    current_plan_stage = next((item for item in plan["stages"] if (item.get("bound_stage_id") or item.get("stage_id")) == current_stage_id), plan["stages"][0])
    checks = _path_checks(root, current_plan_stage)
    evidence = {
        "journal_present": bool(controller is not None and controller.state.get("revision", 0) > 0),
        "git_present": (root / ".git").exists(),
        "source_present": (root / "src").exists(),
        "tests_present": (root / "tests").exists(),
        "outputs_present": (root / "outputs").exists(),
        "canonical_history_used": bool(controller is not None),
        "completion_inferred_from_files": False,
    }
    current_state_written = _write_derived(root / CURRENT_STATE_RELATIVE_PATH, _render_current_state(root, plan, projection, change, checks))
    return {
        "status": "INGESTED",
        "plan_discovery": discovery,
        "requirements_loaded": True,
        "stage_plan_loaded": True,
        "workflow_plan_generated": True,
        "current_state_generated": True,
        "workflow_plan_written": workflow_plan_written,
        "current_state_written": current_state_written,
        "generated_files": [WORKFLOW_PLAN_RELATIVE_PATH.as_posix(), CURRENT_STATE_RELATIVE_PATH.as_posix()],
        "registered_stage_ids": registered,
        "stage_started": started,
        "stages": copy.deepcopy(plan["stages"]),
        "plan_change": change,
        "stage_data_validation": checks,
        "existing_project_evidence": evidence,
        # Keep the original public list stable for callers that already use
        # its first four entries; expose the complete contract order beside
        # it for new-session recovery tooling.
        "context_recovery_order": ["plan/REQUIREMENTS.md", "plan/STAGE_PLAN.md", "plan/WORKFLOW_PLAN.md", "journal", "plan/CURRENT_STATE.md", "latest receipts"],
        "context_recovery_read_order": [
            "docs/outer-loop-contract.md",
            "docs/project-planning.md",
            "plan/REQUIREMENTS.md",
            "plan/STAGE_PLAN.md",
            "plan/WORKFLOW_PLAN.md",
            "canonical journal",
            "latest canonical receipts",
            "plan/CURRENT_STATE.md",
        ],
        "plan_analysis": plan_analysis,
        "current_plan_source": {"requirements": REQUIREMENTS_RELATIVE_PATH.as_posix(), "stage_plan": STAGE_PLAN_RELATIVE_PATH.as_posix(), "requirements_digest": plan["requirements_digest"], "requirements_semantic_digest": plan.get("requirements_semantic_digest"), "stage_plan_digest": plan["stage_plan_digest"]},
        "chatgpt_target_mode": plan.get("chatgpt_target_mode", "DEFAULT"),
        "chatgpt_target_url_digest": plan.get("chatgpt_target_url_digest"),
        "project_target_change": copy.deepcopy(prepared.get("project_target_change")),
        "canonical": projection,
        "human_intervention_count": 0,
        "auto_approved_plan_intake": prepared["auto_approved"],
    }


__all__ = [
    "CURRENT_STATE_RELATIVE_PATH",
    "PLAN_RELATIVE_PATH",
    "ProjectPlanIngestionError",
    "REQUIREMENTS_RELATIVE_PATH",
    "STAGE_PLAN_RELATIVE_PATH",
    "WORKFLOW_PLAN_RELATIVE_PATH",
    "detect_plan_sources",
    "analyze_project_plan",
    "ensure_plan_intake",
    "load_project_plan",
    "sync_project_plan",
]
