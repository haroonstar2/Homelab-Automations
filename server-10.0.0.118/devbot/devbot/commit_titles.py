"""Conventional, reviewable commit titles for code subtasks."""

import re

TITLE_RE = re.compile(r"^(feat|fix|test|refactor|docs|chore)(\([a-z0-9][a-z0-9-]*\))?: [a-z0-9].{2,}$")
GENERIC = ("update files", "apply changes", "fix ci", "review feedback", "continue pr task")


def infer_scope(paths) -> str:
    joined = "\n".join(paths or ())
    for scope in ("core-api", "web-client", "game-server"):
        if f"/{scope}/" in f"/{joined}/" or f"apps/{scope}" in joined:
            return scope
    return "repo"


def validate_commit_title(title: str) -> str:
    title = " ".join(title.strip().split())
    if len(title) > 72:
        raise ValueError("Commit title must be at most 72 characters")
    if not TITLE_RE.fullmatch(title):
        raise ValueError("Commit title must use `type(scope): imperative description`")
    description = title.split(":", 1)[1].strip().lower()
    if any(generic in description for generic in GENERIC):
        raise ValueError("Commit title is too generic")
    return title


def propose_commit_title(request: str, paths=(), kind: str = "feat") -> str:
    candidates = re.findall(
        r"(?im)^\s*(?:[-*]\s*)?(add|create|implement|remove|rename|update|fix)\s+([^\n.]+)", request
    )
    if candidates:
        action, subject = candidates[-1]
        phrase = f"{action.lower()} {subject}"
    else:
        phrase = request.strip().splitlines()[0] if request.strip() else "implement requested change"
    phrase = re.sub(r"[`*_#<>]", "", phrase)
    phrase = re.sub(r"\([^)]*[/\\][^)]*\)", "", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip().lower()
    scope = infer_scope(paths)
    prefix = f"{kind}({scope}): "
    phrase = phrase[: 72 - len(prefix)].rstrip(" -:;,.")
    return validate_commit_title(prefix + phrase)
