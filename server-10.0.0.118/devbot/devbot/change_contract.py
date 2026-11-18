"""Validated file-scope contract for one code-generation subtask."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workspace_paths import checked_path


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _validated_paths(repo_dir: Path, values) -> tuple[str, ...]:
    normalized = []
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Contract paths must be nonempty strings")
        path = checked_path(repo_dir, value.strip())
        relative = path.relative_to(repo_dir.resolve()).as_posix()
        if relative not in normalized:
            normalized.append(relative)
    return tuple(normalized)


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    changed_paths: tuple[str, ...]
    missing_required: tuple[str, ...]
    unexpected_paths: tuple[str, ...]
    empty_required_paths: tuple[str, ...]
    protected_paths_changed: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "changed_paths": list(self.changed_paths),
            "missing_required": list(self.missing_required),
            "unexpected_paths": list(self.unexpected_paths),
            "empty_required_paths": list(self.empty_required_paths),
            "protected_paths_changed": list(self.protected_paths_changed),
        }

    def failure_message(self) -> str:
        reasons = []
        if not self.changed_paths:
            reasons.append("no files changed")
        if self.missing_required:
            reasons.append("required paths not changed: " + ", ".join(self.missing_required))
        if self.unexpected_paths:
            reasons.append("paths outside the allowed scope changed: " + ", ".join(self.unexpected_paths))
        if self.empty_required_paths:
            reasons.append("required new paths are empty: " + ", ".join(self.empty_required_paths))
        if self.protected_paths_changed:
            reasons.append("protected verification paths changed: " + ", ".join(self.protected_paths_changed))
        return "; ".join(reasons) or "change contract failed"


@dataclass(frozen=True)
class ChangeContract:
    repo_dir: Path
    required_paths: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    initially_missing_required: tuple[str, ...] = ()

    @classmethod
    def create(cls, repo_dir: Path, required_paths=(), allowed_paths=None, protected_paths=()) -> "ChangeContract":
        required = _validated_paths(repo_dir, required_paths)
        if allowed_paths is None:
            raise ValueError("An explicit allowed file scope is required")
        allowed = _validated_paths(repo_dir, allowed_paths)
        if not allowed:
            raise ValueError("The allowed file scope must contain at least one path")
        protected = _validated_paths(repo_dir, protected_paths)
        missing_from_allowlist = sorted(set(required) - set(allowed))
        if missing_from_allowlist:
            raise ValueError(
                "Required paths must be allowed: " + ", ".join(missing_from_allowlist)
            )
        protected_in_scope = sorted(path for path in allowed if _under(path, protected))
        if protected_in_scope:
            raise ValueError(
                "Protected verification paths cannot be approved for model edits: "
                + ", ".join(protected_in_scope)
            )
        missing = tuple(path for path in required if not checked_path(repo_dir, path).exists())
        return cls(repo_dir.resolve(), required, allowed, protected, missing)

    def add_targets(self, selected: list[str]) -> list[str]:
        result = list(selected)
        allowed = set(self.allowed_paths)
        result = [path for path in result if path in allowed]
        for path in self.required_paths:
            if path not in result:
                result.append(path)
        return result

    def evaluate(self, changed_paths) -> ContractResult:
        changed = tuple(sorted({str(path).replace("\\", "/") for path in changed_paths if path}))
        changed_set = set(changed)
        missing = tuple(sorted(set(self.required_paths) - changed_set))
        unexpected = tuple(sorted(changed_set - set(self.allowed_paths)))
        protected_changed = tuple(sorted(path for path in changed_set if _under(path, self.protected_paths)))
        empty_required = tuple(
            sorted(
                path for path in self.initially_missing_required
                if path in changed_set and (
                    not checked_path(self.repo_dir, path).is_file()
                    or checked_path(self.repo_dir, path).stat().st_size == 0
                )
            )
        )
        return ContractResult(
            bool(changed) and not missing and not unexpected and not empty_required and not protected_changed,
            changed, missing, unexpected, empty_required, protected_changed,
        )
