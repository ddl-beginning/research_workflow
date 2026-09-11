"""Small immutable review-artifact registry.

Artifacts are evidence, not execution inputs.  The registry records a role,
lane, content hash and user-viewable reference for each file.  It accepts
images, reports, CAD files, CLI output, rendered HTML and test artifacts; the
extension is informational and never limits registration.

The registry is append-only from the point of view of a Stage.  A primary
record and a challenger record therefore remain distinct even when they have
the same basename.  A changed file or a tampered manifest fails closed when
the registry is loaded or validated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, validate_against_schema


ARTIFACT_ROLES = (
    "representative_good",
    "representative_hard",
    "worst_case",
    "before_after",
    "metrics_summary",
)
LANES = ("primary", "challenger")


class ArtifactRegistryError(ContractValidationError):
    """Raised for missing, invalid or tampered review artifacts."""


def _text(value: Any, field: str, *, required: bool = True, max_length: int = 512) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ArtifactRegistryError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ArtifactRegistryError(f"{field} must be non-empty")
    if len(value) > max_length or "\x00" in value:
        raise ArtifactRegistryError(f"{field} is invalid or too long")
    return value


def _safe_root(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ArtifactRegistryError(f"artifact root is not a directory: {root}")
    return root


def _hash_file(path: Path) -> tuple[str, int]:
    if not path.is_file():
        raise ArtifactRegistryError(f"artifact file does not exist: {path}")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactRegistryError(f"cannot read artifact: {path.name}") from exc
    return digest.hexdigest(), size


def _within(path: Path, root: Path | None) -> None:
    if root is None:
        return
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ArtifactRegistryError("artifact path escapes the configured root") from exc


def _artifact_id(stage_id: str, lane: str, role: str, digest: str) -> str:
    body = {"stage_id": stage_id, "lane": lane, "role": role, "sha256": digest}
    return "artifact-" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:20]


def _artifact_ref(stage_id: str, lane: str, role: str, artifact_id: str) -> str:
    return f"artifact://{stage_id}/{lane}/{role}/{artifact_id}"


class ArtifactRegistry:
    """Append-only registry for one Stage's review evidence."""

    def __init__(
        self,
        stage_id: str,
        *,
        root: str | os.PathLike[str] | None = None,
        manifest_path: str | os.PathLike[str] | None = None,
        required_roles: Sequence[str] | None = None,
    ) -> None:
        self.stage_id = _text(stage_id, "stage_id", max_length=256)
        self.root = _safe_root(root)
        self.manifest_path = Path(manifest_path).expanduser().resolve() if manifest_path else None
        roles = list(required_roles or [])
        invalid = [role for role in roles if role not in ARTIFACT_ROLES]
        if invalid:
            raise ArtifactRegistryError(f"unknown artifact role(s): {invalid!r}")
        if len(set(roles)) != len(roles):
            raise ArtifactRegistryError("required artifact roles must be unique")
        self.required_roles = roles
        self._artifacts: list[dict[str, Any]] = []
        self._manifest_digest: str | None = None
        if self.manifest_path and self.manifest_path.exists():
            self._load()

    @property
    def artifacts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._artifacts)

    def register(
        self,
        path: str | os.PathLike[str],
        role: str,
        *,
        lane: str = "primary",
        media_type: str | None = None,
        uri: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register one existing file without copying or overwriting it."""

        role = _text(role, "role", max_length=64)
        if role not in ARTIFACT_ROLES:
            raise ArtifactRegistryError(f"unknown artifact role: {role}")
        lane = _text(lane, "lane", max_length=32).lower()
        if lane not in LANES:
            raise ArtifactRegistryError("lane must be primary or challenger")
        file_path = Path(path).expanduser().resolve()
        _within(file_path, self.root)
        digest, size = _hash_file(file_path)
        artifact_id = _artifact_id(self.stage_id, lane, role, digest)
        # The same lane/role may only point at one immutable content record.
        # A second result gets a new role or a new Stage iteration instead of
        # silently replacing the first evidence.
        for item in self._artifacts:
            if item["lane"] == lane and item["role"] == role:
                raise ArtifactRegistryError(
                    f"artifact role already registered for lane {lane}: {role}"
                )
        safe_metadata = _safe_metadata(metadata or {})
        record: dict[str, Any] = {
            "artifact_id": artifact_id,
            "stage_id": self.stage_id,
            "lane": lane,
            "role": role,
            "path": str(file_path),
            "basename": file_path.name,
            "media_type": _text(media_type, "media_type", required=False, max_length=128),
            "sha256": digest,
            "size_bytes": size,
            "uri": _text(uri, "uri", required=False, max_length=1024)
            or _artifact_ref(self.stage_id, lane, role, artifact_id),
            "metadata": safe_metadata,
        }
        self._artifacts.append(record)
        self._artifacts.sort(key=lambda item: item["artifact_id"])
        return copy.deepcopy(record)

    add = register
    register_artifact = register

    def manifest(self) -> dict[str, Any]:
        body = {
            "manifest_version": "artifact_manifest.v1",
            "stage_id": self.stage_id,
            "required_roles": list(self.required_roles),
            "artifacts": copy.deepcopy(self._artifacts),
        }
        digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        return {**body, "manifest_digest": digest}

    def validate(self, *, require_roles: bool = False) -> dict[str, Any]:
        manifest = self.manifest()
        validate_artifact_manifest(manifest, check_files=True)
        if require_roles:
            roles = {item["role"] for item in self._artifacts}
            missing = sorted(set(self.required_roles) - roles)
            if missing:
                raise ArtifactRegistryError(f"missing required artifact role(s): {missing!r}")
        return manifest

    def save(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        destination = Path(path).expanduser().resolve() if path else self.manifest_path
        if destination is None:
            raise ArtifactRegistryError("manifest path is required")
        manifest = self.validate()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactRegistryError("artifact manifest is unreadable") from exc
            validate_artifact_manifest(existing, check_files=False)
            if existing.get("manifest_digest") != manifest["manifest_digest"]:
                raise ArtifactRegistryError("refusing to overwrite a different artifact manifest")
            return existing
        payload = canonical_json(manifest) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, destination)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise ArtifactRegistryError("cannot persist artifact manifest") from exc
        self.manifest_path = destination
        self._manifest_digest = manifest["manifest_digest"]
        return manifest

    def _load(self) -> None:
        assert self.manifest_path is not None
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactRegistryError("artifact manifest is unreadable") from exc
        # Check scope before opening any file named by an untrusted manifest.
        checked = validate_artifact_manifest(payload, check_files=False)
        if checked["stage_id"] != self.stage_id:
            raise ArtifactRegistryError("artifact manifest stage_id does not match registry")
        for item in checked["artifacts"]:
            _within(Path(item["path"]).expanduser().resolve(), self.root)
        checked = validate_artifact_manifest(checked, check_files=True)
        self._artifacts = copy.deepcopy(checked["artifacts"])
        self.required_roles = list(checked.get("required_roles", self.required_roles))
        self._manifest_digest = checked["manifest_digest"]

    def resolve(self, reference: str) -> dict[str, Any]:
        reference = _text(reference, "reference", max_length=2048)
        for item in self._artifacts:
            if reference in {item["artifact_id"], item["uri"], item["path"]}:
                digest, _ = _hash_file(Path(item["path"]))
                if digest != item["sha256"]:
                    raise ArtifactRegistryError("artifact content digest mismatch")
                return copy.deepcopy(item)
        raise ArtifactRegistryError("unknown artifact reference")


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    sensitive = {
        "cookie", "cookies", "token", "tokens", "authorization", "session",
        "session_storage", "storage_state", "raw_dom", "dom", "prompt",
    }
    def visit(item: Any, path: str) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                name = str(key)
                if name.lower() in sensitive:
                    raise ArtifactRegistryError(f"sensitive metadata key is not allowed: {path}.{name}")
                result[name] = visit(child, f"{path}.{name}")
            return result
        if isinstance(item, list):
            return [visit(child, f"{path}[]") for child in item]
        if isinstance(item, str):
            if len(item) > 4096 or "\x00" in item:
                raise ArtifactRegistryError(f"artifact metadata string is too long at {path}")
            return item
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        raise ArtifactRegistryError(f"unsupported artifact metadata value at {path}")
    checked = visit(value, "metadata")
    assert isinstance(checked, dict)
    return checked


def validate_artifact_manifest(
    manifest: Mapping[str, Any],
    *,
    check_files: bool = False,
) -> dict[str, Any]:
    """Validate manifest hash and optionally every referenced artifact file."""

    if not isinstance(manifest, Mapping):
        raise ArtifactRegistryError("artifact manifest must be an object")
    checked = copy.deepcopy(dict(manifest))
    validate_against_schema(checked, "artifact_manifest")
    required_roles = checked.get("required_roles", [])
    if len(set(required_roles)) != len(required_roles) or any(
        role not in ARTIFACT_ROLES for role in required_roles
    ):
        raise ArtifactRegistryError("required_roles contains an unknown or duplicate role")
    supplied = checked.pop("manifest_digest", None)
    expected = hashlib.sha256(canonical_json(checked).encode("utf-8")).hexdigest()
    if not isinstance(supplied, str) or supplied != expected:
        raise ArtifactRegistryError("artifact manifest digest mismatch")
    stage_id = checked["stage_id"]
    artifacts = checked["artifacts"]
    seen_ids: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()
    for item in artifacts:
        artifact_id = item["artifact_id"]
        if artifact_id in seen_ids:
            raise ArtifactRegistryError("duplicate artifact_id")
        seen_ids.add(artifact_id)
        if item["stage_id"] != stage_id:
            raise ArtifactRegistryError("artifact stage_id mismatch")
        if Path(item["path"]).name != item["basename"]:
            raise ArtifactRegistryError("artifact basename does not match path")
        lane_role = (item["lane"], item["role"])
        if lane_role in seen_roles:
            raise ArtifactRegistryError("duplicate lane/role artifact")
        seen_roles.add(lane_role)
        # Recompute the identity so a tampered lane/role/path cannot pass only
        # because its file hash happens to be unchanged.
        if artifact_id != _artifact_id(stage_id, item["lane"], item["role"], item["sha256"]):
            raise ArtifactRegistryError("artifact_id does not match content identity")
        _safe_metadata(item.get("metadata", {}))
        if check_files:
            digest, size = _hash_file(Path(item["path"]))
            if digest != item["sha256"] or size != item["size_bytes"]:
                raise ArtifactRegistryError(f"artifact content tampered: {item['basename']}")
    return {**checked, "manifest_digest": expected}


def required_review_artifact_refs(
    manifest: Mapping[str, Any],
    required_roles: Sequence[str] | None = None,
) -> list[str]:
    checked = validate_artifact_manifest(manifest, check_files=False)
    requested = list(required_roles or checked.get("required_roles", []))
    if any(role not in ARTIFACT_ROLES for role in requested):
        raise ArtifactRegistryError("unknown required artifact role")
    by_role = {item["role"]: item for item in checked["artifacts"]}
    missing = sorted(set(requested) - set(by_role))
    if missing:
        raise ArtifactRegistryError(f"missing required artifact role(s): {missing!r}")
    return [by_role[role]["uri"] for role in requested]


__all__ = [
    "ARTIFACT_ROLES",
    "ArtifactRegistry",
    "ArtifactRegistryError",
    "LANES",
    "required_review_artifact_refs",
    "validate_artifact_manifest",
]
