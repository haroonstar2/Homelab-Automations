"""Path containment for model-supplied file operations."""

from pathlib import Path, PureWindowsPath


def checked_path(root: Path, value: str, *, absolute: bool = False) -> Path:
    root = root.resolve()
    path = Path(value)
    if not absolute and (path.is_absolute() or PureWindowsPath(value).drive or "\\" in value):
        raise ValueError(f"Expected a repository-relative POSIX path: {value}")
    if ".." in path.parts or any(part.lower() == ".git" for part in path.parts):
        raise ValueError(f"Forbidden repository path: {value}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(f"Path escapes repository: {value}")
    return resolved
