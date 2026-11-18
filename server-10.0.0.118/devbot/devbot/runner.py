"""Isolated code-generation runner for /code.

Flow:
1. Clone target GitHub repo into an isolated scratch folder (/data/runner-scratch/<thread_id>/repo).
2. Build a repository snapshot prioritizing files mentioned in the task so the LLM drafts an accurate plan.
3. After the owner approves the plan in Discord:
   - Check out a fresh branch `agent/<slug>` (never main/master).
   - Select the exact target files referenced in the plan and task (ranked by specificity).
   - Run `aider` with `--edit-format diff` on those target files.
   - Treat generation errors or no file modifications as failure; never fall back to whole-file output.
   - Run verification (`go test ./...`, `python -m compileall`), and if verification fails, run a
     self-healing repair pass with the compiler/test error output.
   - Commit and push `agent/<slug>` to GitHub, then open a Draft PR.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import signal
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable

from .change_contract import ChangeContract
from .commit_titles import validate_commit_title
from .prompts import CODE_STYLE_CONVENTIONS

log = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".mypy_cache", "generated", "coverage",
}
AIDER_EDIT_TIMEOUT = 900
CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".sql",
    ".yml", ".yaml", ".toml", ".json", ".md",
}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock"}
GENERIC_FILENAMES = {
    "package.json", "tsconfig.json", "tsconfig.build.json", "eslint.config.mjs",
    "postcss.config.mjs", "next.config.ts", "jest.config.mjs", "jest.setup.js",
    "main.ts", "main.go", "index.ts", "index.js", "layout.tsx", "readme.md",
}
PLANNING_CONTEXT_NAMES = {
    "agents.md", "readme.md", "readme.rst", "go.mod", "go.work", "package.json",
    "pyproject.toml", "requirements.txt", "cargo.toml", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", ".aider.conf.yml",
}


@dataclass(frozen=True)
class RunOutcome:
    ok: bool
    branch: str
    commit_sha: str
    diff_stat: str
    test_output: str
    error: str = ""
    base_sha: str = ""
    cumulative_diff_stat: str = ""
    verification: dict | None = None
    change_contract: dict | None = None


def _verification_excerpt(report, output: str) -> str:
    return report.failure_summary() if hasattr(report, "failure_summary") else output[-900:]


async def _run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process(proc)
        return 124, f"Command timed out after {timeout}s: {' '.join(cmd[:3])}"
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process(proc))
        raise
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


async def _terminate_process(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        # Aider already depends on psutil. Killing descendants first closes any
        # inherited stdout handles so proc.communicate()/wait cannot hang.
        import psutil

        try:
            parent = psutil.Process(proc.pid)
            descendants = parent.children(recursive=True)
            for child in reversed(descendants):
                child.kill()
            parent.kill()
            await asyncio.to_thread(psutil.wait_procs, [*descendants, parent], timeout=5)
        except psutil.NoSuchProcess:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def _all_repo_files(repo_dir: Path) -> list[Path]:
    rel_files: list[Path] = []
    for root, dirs, filenames in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_root = Path(root).relative_to(repo_dir)
        for fn in sorted(filenames):
            if fn.startswith(".aider") or fn in SKIP_FILES:
                continue
            rel = (rel_root / fn) if str(rel_root) != "." else Path(fn)
            rel_files.append(rel)
    return rel_files


def score_file_relevance(rel: Path, text: str) -> int:
    """Return a relevance score (lower is better; 99 means not specifically matched)."""
    q_lower = text.lower()
    posix = str(rel).replace("\\", "/")
    posix_l = posix.lower()
    name_l = rel.name.lower()
    stem_l = rel.stem.lower()
    parent_l = str(rel.parent).replace("\\", "/").lower()

    # Tier 0: Exact relative path appears in text (e.g. "apps/game-server/internal/ws/hub.go")
    if posix_l in q_lower:
        return 0

    # Tier 1: Non-generic filename or component stem appears in text (e.g. "hub.go", "GameActionBar", "GamePage")
    if name_l not in GENERIC_FILENAMES:
        if name_l in q_lower:
            return 1
        if len(stem_l) >= 5 and re.search(r"(?<![a-z0-9])" + re.escape(stem_l) + r"(?![a-z0-9])", q_lower):
            return 1

    # Tier 2: Special case for Next.js route pages (e.g. "GamePage" -> "app/game/[id]/page.tsx" or "app/game/page.tsx")
    if name_l == "page.tsx" and "gamepage" in q_lower and "/game" in posix_l:
        return 1

    # Tier 3: File is inside a specific subdirectory explicitly mentioned (e.g. "apps/game-server/internal/game")
    if parent_l not in ("", ".") and parent_l.count("/") >= 2 and parent_l in q_lower:
        if rel.suffix.lower() in {".go", ".ts", ".tsx", ".py", ".rs", ".java", ".cs"}:
            return 2

    return 99


def select_target_files(repo_dir: Path, text: str, max_files: int = 8) -> list[str]:
    """Return ranked relative paths of repo files specifically targeted by the task/plan."""
    tier01: list[tuple[int, int, str]] = []
    tier23: list[tuple[int, int, str]] = []
    for rel in _all_repo_files(repo_dir):
        if rel.suffix.lower() not in CODE_EXTS:
            continue
        score = score_file_relevance(rel, text)
        if score < 99:
            posix = str(rel).replace("\\", "/")
            is_test = 1 if ("_test." in posix or ".test." in posix or ".spec." in posix) else 0
            if score <= 1:
                tier01.append((score, is_test, posix))
            else:
                tier23.append((score, is_test, posix))

    tier01.sort()
    combined = tier01 + sorted(tier23)
    return [posix for _, _, posix in combined[:max_files]]


def build_repo_context(repo_dir: Path, query_text: str = "", max_chars: int = 18000) -> str:
    """Build a rich repository snapshot (file tree + matched source files + README) for /ask and /code."""
    rel_files = _all_repo_files(repo_dir)
    tree_lines = [f"- {str(p).replace(chr(92), '/')}" for p in rel_files[:180]]
    sections: list[str] = [f"Repository File Tree ({len(rel_files)} files total):\n" + "\n".join(tree_lines)]
    used = len(sections[0])

    def priority_key(rel: Path) -> tuple[int, int, str]:
        posix = str(rel).replace("\\", "/")
        rel_score = score_file_relevance(rel, query_text)
        if rel_score < 99:
            return (rel_score, len(posix), posix)
        name_l = rel.name.lower()
        if name_l.startswith("readme"):
            return (10, len(posix), posix)
        if name_l in ("package.json", "pyproject.toml", "cargo.toml", "go.mod", "docker-compose.yml"):
            return (11, len(posix), posix)
        if rel.suffix.lower() in CODE_EXTS:
            return (12, len(posix), posix)
        return (99, len(posix), posix)

    for rel in sorted(rel_files, key=priority_key):
        if rel.suffix.lower() not in CODE_EXTS and not rel.name.lower().startswith("readme"):
            continue
        full = repo_dir / rel
        try:
            if not full.is_file() or full.stat().st_size > 120_000:
                continue
            content = full.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not content:
            continue
        posix = str(rel).replace("\\", "/")
        # Give matched target files a generous 4500-char window so full structs/methods are visible
        limit = 4500 if score_file_relevance(rel, query_text) < 99 else 2000
        snippet = content[:limit] + ("\n... [truncated]" if len(content) > limit else "")
        block = f"\n\n--- {posix} ---\n{snippet}"
        if used + len(block) > max_chars:
            break
        sections.append(block)
        used += len(block)

    return "".join(sections)


def extract_package_symbols(sibling_files: dict[str, str], exclude_rel: str = "") -> str:
    """Extract top-level declarations from sibling files so the LLM never redeclares existing symbols."""
    lines_out: list[str] = []
    for path, content in sorted(sibling_files.items()):
        if path == exclude_rel:
            continue
        decls: list[str] = []
        in_const_block = False
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if path.endswith(".go"):
                if stripped.startswith(("func ", "type ")):
                    decls.append(stripped.rstrip("{").strip())
                elif stripped.startswith("const ("):
                    in_const_block = True
                elif in_const_block:
                    if stripped == ")":
                        in_const_block = False
                    elif stripped and not stripped.startswith("//"):
                        decls.append(f"const {stripped.split('=')[0].strip()}")
                elif stripped.startswith(("const ", "var ")):
                    decls.append(stripped.split("=")[0].strip())
            elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
                if stripped.startswith(("export function ", "export async function ", "export interface ", "export type ", "export class ", "export const ")):
                    decls.append(stripped.rstrip("{").strip()[:100])
            elif path.endswith(".py"):
                if raw_line.startswith(("def ", "async def ", "class ")):
                    decls.append(stripped.rstrip(":").strip())
        if decls:
            lines_out.append(f"• {path}: " + " | ".join(decls[:30]))
    if not lines_out:
        return ""
    return "=== EXISTING DECLARATIONS IN SIBLING FILES (DO NOT REDECLARE) ===\n" + "\n".join(lines_out)


def detect_stack_versions(repo_dir: Path) -> list[tuple[str, str, str]]:
    """Inspect go.mod, package.json, and pyproject.toml to return [(ecosystem, name, version), ...]."""
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(eco: str, name: str, ver: str) -> None:
        key = (eco, name)
        if key not in seen:
            seen.add(key)
            found.append((eco, name, ver))

    # 1. Go modules
    for gm in sorted(repo_dir.rglob("go.mod")):
        if "node_modules" in gm.parts:
            continue
        text = gm.read_text(encoding="utf-8", errors="replace")
        m_go = re.search(r"^go\s+(\S+)", text, re.MULTILINE)
        if m_go:
            add("go", "go", m_go.group(1))
        for line in text.splitlines():
            line = line.strip()
            single = re.match(r"require\s+(\S+)\s+(\S+)", line)
            if single:
                add("go", single.group(1), single.group(2))
                continue
            if not line or line.startswith(("//", "module ", "go ", "require", ")", "(")) or "// indirect" in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and "/" in parts[0]:
                add("go", parts[0], parts[1])

    # 2. Node / TypeScript packages
    priority_node = {"next", "react", "typescript", "tailwindcss", "prisma", "@prisma/client", "@nestjs/core", "socket.io"}
    locked_node: dict[str, str] = {}
    for lock in sorted(repo_dir.rglob("package-lock.json")):
        if "node_modules" in lock.parts:
            continue
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for pkg in priority_node:
            entry = (data.get("packages") or {}).get(f"node_modules/{pkg}") or (data.get("dependencies") or {}).get(pkg)
            if isinstance(entry, dict) and entry.get("version"):
                locked_node[pkg] = str(entry["version"])
    for pj in sorted(repo_dir.rglob("package.json")):
        if "node_modules" in pj.parts:
            continue
        try:
            data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for pkg in sorted(deps):
            if pkg in priority_node:
                add("node", pkg, locked_node.get(pkg, f"declared:{deps[pkg]}"))

    # 3. Python projects. Prefer lock/resolution files when present; otherwise label declarations.
    for pyproject in sorted(repo_dir.rglob("pyproject.toml")):
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        dependencies = list((data.get("project") or {}).get("dependencies") or [])
        for dep in dependencies:
            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)", dep)
            if match:
                add("python", match.group(1).lower(), f"declared:{match.group(2) or '*'}")
    for requirements in sorted(repo_dir.rglob("requirements*.txt")):
        for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(==|~=|>=|<=|>|<)?\s*([^;#\s]*)", line)
            if match and match.group(1):
                label = match.group(3) if match.group(2) == "==" else f"declared:{(match.group(2) or '') + (match.group(3) or '*')}"
                add("python", match.group(1).lower(), label)

    return found[:25]


async def extract_go_doc_context(repo_dir: Path, target_files: list[str], max_chars: int = 3500) -> str:
    """Run `go doc` on non-stdlib packages imported by target .go files to get exact API signatures."""
    go_mods = [p for p in repo_dir.rglob("go.mod") if "node_modules" not in p.parts]
    if not go_mods:
        return ""

    imports_by_root: dict[Path, set[str]] = {}
    for rel in target_files:
        if not rel.endswith(".go"):
            continue
        p = repo_dir / rel
        if not p.is_file():
            continue
        go_root = next((parent for parent in (p.parent, *p.parents) if (parent / "go.mod").is_file()), None)
        if not go_root or not go_root.is_relative_to(repo_dir):
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        for m in re.findall(r'"([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/[^"]+)"', content):
            if "/internal/" not in m:
                imports_by_root.setdefault(go_root, set()).add(m)

    if not imports_by_root:
        return ""

    parts: list[str] = []
    used = 0
    for go_root, packages in imports_by_root.items():
        for pkg in sorted(packages)[:5]:
            code, out = await _run_cmd(["go", "doc", "-short", pkg], cwd=go_root, timeout=30)
            if code == 0 and out.strip():
                snippet = out.strip()[:1200]
                block = f"--- go doc {pkg} (module {go_root.relative_to(repo_dir)}) ---\n{snippet}"
                if used + len(block) > max_chars:
                    return "\n\n".join(parts)
                parts.append(block)
                used += len(block)
    return "\n\n".join(parts)


class CodeRunner:
    def __init__(
        self, data_dir: Path, github_token: str, llm_base_url: str, llm_api_key: str,
        context_tokens: int = 16384, output_tokens: int = 4096,
        ollama_url: str = "", ollama_model: str = "",
        verifier_docker_host: str = "unix:///run/rootless-docker/docker.sock",
        verifier_host_data_dir: Path | None = None,
    ) -> None:
        if context_tokens < 4096 or output_tokens < 512 or output_tokens >= context_tokens:
            raise ValueError("Invalid LLM context/output token budget")
        self.scratch_root = data_dir / "runner-scratch"
        self.checks_file = data_dir / "project-checks.json"
        self.github_token = github_token.strip()
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key
        self.context_tokens = context_tokens
        self.output_tokens = output_tokens
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model
        from .container_verifier import ContainerVerifier
        self.verifier = ContainerVerifier(
            data_dir, verifier_host_data_dir or data_dir, verifier_docker_host, _run_cmd,
        )
        self.active_status: dict[int, str] = {}

    @staticmethod
    def _remote_url(repo_full: str) -> str:
        return f"https://github.com/{repo_full}.git"

    def _git_auth_env(self) -> dict[str, str]:
        """Authenticate Git over HTTPS without putting the token in argv or repository config."""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if not self.github_token:
            return env
        credentials = base64.b64encode(
            f"x-access-token:{self.github_token}".encode("utf-8")
        ).decode("ascii")
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        })
        return env

    def _redact(self, text: str) -> str:
        if self.github_token:
            return text.replace(self.github_token, "***")
        return text

    async def _git(self, repo_dir: Path, *args: str, authenticated: bool = False) -> str:
        code, output = await _run_cmd(
            ["git", *args],
            cwd=repo_dir,
            env=self._git_auth_env() if authenticated else None,
            timeout=180,
        )
        if code:
            raise RuntimeError(f"git {args[0]} failed (exit {code}): {self._redact(output)}")
        return output.strip()

    async def _changed_paths_since(self, repo_dir: Path, base_sha: str) -> set[str]:
        tracked = await self._git(repo_dir, "diff", "--name-only", "-z", base_sha, "--")
        untracked = await self._git(repo_dir, "ls-files", "--others", "--exclude-standard", "-z")
        return {
            path.replace("\\", "/")
            for path in (*tracked.split("\0"), *untracked.split("\0"))
            if path
        }

    async def _worktree_fingerprint(self, repo_dir: Path) -> str:
        """Hash every publishable tracked diff and untracked file without following symlinks."""
        tracked_diff = await self._git(
            repo_dir, "diff", "--binary", "--no-ext-diff", "HEAD", "--",
        )
        untracked = await self._git(
            repo_dir, "ls-files", "--others", "--exclude-standard", "-z",
        )

        def digest() -> str:
            result = hashlib.sha256(tracked_diff.encode("utf-8", errors="surrogatepass"))
            for relative in sorted(path for path in untracked.split("\0") if path):
                result.update(b"\0path\0")
                result.update(relative.encode("utf-8", errors="surrogatepass"))
                path = repo_dir / relative
                stat = path.lstat()
                result.update(str(stat.st_mode).encode("ascii"))
                if path.is_symlink():
                    result.update(b"\0symlink\0")
                    result.update(os.readlink(path).encode("utf-8", errors="surrogatepass"))
                elif path.is_file():
                    with path.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            result.update(block)
                else:
                    result.update(b"\0special\0")
            return result.hexdigest()

        return await asyncio.to_thread(digest)

    async def _cumulative_diff_stat(self, repo_dir: Path) -> str:
        code, remote_head = await _run_cmd(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_dir, timeout=30,
        )
        if code or not remote_head.strip():
            return "Default branch reference unavailable"
        return await self._git(repo_dir, "diff", "--stat", f"{remote_head.strip()}...HEAD")

    def _artifact(self, task_dir: Path, name: str, content) -> None:
        artifact_dir = task_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(content, indent=2, default=str) if not isinstance(content, str) else content
        text = self._redact(text)
        llm_api_key = getattr(self, "llm_api_key", "")
        if llm_api_key:
            text = text.replace(llm_api_key, "***")
        temporary = artifact_dir / f".{name}.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(artifact_dir / name)

    async def _assert_branch(self, repo_dir: Path, branch: str) -> None:
        if not branch.startswith("agent/"):
            raise ValueError("Only agent/ branches may be changed or published")
        current = await self._git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            raise RuntimeError(f"Expected {branch}, found {current}; refusing to change or push another branch")

    async def _prepare_branch(self, repo_dir: Path, branch: str) -> None:
        if not branch.startswith("agent/"):
            raise ValueError("Expected agent/ branch")
        await self._git(repo_dir, "config", "user.name", "devbot")
        await self._git(repo_dir, "config", "user.email", "devbot@homelab.local")
        code, output = await _run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_dir)
        if code not in (0, 1):
            raise RuntimeError(self._redact(output))
        await self._git(repo_dir, "checkout", *([] if code == 0 else ["-b"]), branch)
        await self._assert_branch(repo_dir, branch)

    async def _discard_scratch_changes(self, repo_dir: Path) -> None:
        """Restore the isolated runner clone before starting a newly approved subtask."""
        await self._git(repo_dir, "reset", "--hard", "HEAD")
        await self._git(repo_dir, "clean", "-fd")

    async def _sync_branch(self, repo_dir: Path, branch: str) -> None:
        await self._assert_branch(repo_dir, branch)
        remote = await self._git(
            repo_dir, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", authenticated=True,
        )
        if remote:
            await self._git(
                repo_dir, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                authenticated=True,
            )
            await self._git(repo_dir, "merge", "--ff-only", f"origin/{branch}")

    async def prepare_workspace(self, thread_id: int, repo_full: str, task_hint: str = "") -> tuple[Path, str]:
        """Clone repo into scratch dir and return (repo_dir, repo_outline)."""
        task_dir = self.scratch_root / str(thread_id)
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        task_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = task_dir / "repo"

        code, out = await _run_cmd(
            ["git", "clone", "--depth", "20", self._remote_url(repo_full), str(repo_dir)],
            env=self._git_auth_env(),
            timeout=180,
        )
        if code != 0:
            raise RuntimeError(f"git clone failed: {self._redact(out)[:400]}")

        outline = await self.build_planning_context(repo_dir, task_hint)
        return repo_dir, outline

    async def prepare_existing_workspace(
        self, thread_id: int, repo_full: str, branch: str, task_hint: str = "",
    ) -> tuple[Path, str]:
        """Restore and fast-forward an existing agent branch for a continued PR subtask."""
        repo_dir = self.scratch_root / str(thread_id) / "repo"
        if not repo_dir.exists():
            repo_dir, _ = await self.prepare_workspace(thread_id, repo_full, task_hint=task_hint)
            await self._git(
                repo_dir, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                authenticated=True,
            )
            await self._git(repo_dir, "checkout", "-b", branch, f"origin/{branch}")
        await self._assert_branch(repo_dir, branch)
        await self._discard_scratch_changes(repo_dir)
        await self._git(repo_dir, "remote", "set-url", "origin", self._remote_url(repo_full))
        await self._sync_branch(repo_dir, branch)
        return repo_dir, await self.build_planning_context(repo_dir, task_hint)

    async def build_planning_context(self, repo_dir: Path, query_text: str) -> str:
        """Aider symbol map plus exact, owner-authored instructions and build manifests."""
        task_dir = repo_dir.parent
        task_dir.mkdir(parents=True, exist_ok=True)
        repo_files = _all_repo_files(repo_dir)
        with tempfile.TemporaryDirectory(prefix="repo-map-", dir=task_dir) as tmp:
            request = Path(tmp) / "request.json"
            output = Path(tmp) / "map.txt"
            mentioned = [
                str(rel).replace("\\", "/") for rel in repo_files
                if str(rel).replace("\\", "/").lower() in query_text.lower()
                or rel.name.lower() in query_text.lower()
            ]
            request.write_text(json.dumps({
                "action": "repository_map", "artifact_dir": str(task_dir / "aider-map-cache"),
                "output_file": str(output), "map_tokens": 4500,
                "files": [str(rel).replace("\\", "/") for rel in repo_files],
                "mentioned_fnames": mentioned,
            }), encoding="utf-8")
            code, command_output = await _run_cmd(
                [sys.executable, str(Path(__file__).with_name("aider_worker.py")), str(request)],
                cwd=repo_dir, timeout=180,
            )
            if code:
                raise RuntimeError(f"Aider repository map failed: {command_output[-1000:]}")
            repo_map = output.read_text(encoding="utf-8")

        exact = []
        used = 0
        for rel in repo_files:
            posix = str(rel).replace("\\", "/")
            if rel.name.lower() not in PLANNING_CONTEXT_NAMES:
                continue
            content = (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
            block = f"--- exact file: {posix} ---\n{content}\n"
            if used + len(block) > 12000:
                continue
            exact.append(block)
            used += len(block)
        return "Repository symbol map (generated by Aider):\n" + repo_map + "\n\n" + "\n".join(exact)

    async def inspect_repo(self, repo_full: str, query_text: str = "") -> str:
        """Clone or update repo_full in a shared cache dir and return a deep repo snapshot for /ask."""
        safe = repo_full.replace("/", "__")
        cache_dir = self.scratch_root / "ask-cache" / safe
        if (cache_dir / ".git").exists():
            await _run_cmd(
                ["git", "pull", "--ff-only"], cwd=cache_dir,
                env=self._git_auth_env(), timeout=60,
            )
        else:
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            code, out = await _run_cmd(
                ["git", "clone", "--depth", "10", self._remote_url(repo_full), str(cache_dir)],
                env=self._git_auth_env(),
                timeout=120,
            )
            if code != 0:
                raise RuntimeError(f"git clone failed for {repo_full}: {self._redact(out)[:300]}")
        return await asyncio.to_thread(build_repo_context, cache_dir, query_text)

    async def execute_task(
        self,
        thread_id: int,
        repo_full: str,
        branch: str,
        task: str,
        plan: str,
        conventions: str = "",
        ground_truth: str = "",
        model: str = "smart",
        commit_title: str = "",
        required_paths: tuple[str, ...] | list[str] = (),
        allowed_paths: tuple[str, ...] | list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_think: Callable[[str], Awaitable[None]] | None = None,
    ) -> RunOutcome:
        """Run per-file surgical edits & self-healing test loop on `agent/<slug>` with live Discord updates."""
        if not branch.startswith("agent/"):
            raise ValueError(f"Refusing to run on non-agent branch: {branch}")

        steps_log: list[str] = [f"🛠️ **Running Code Task on `{repo_full}` → `{branch}`**"]

        async def emit(line: str) -> None:
            self.active_status[thread_id] = line
            steps_log.append(line)
            if on_progress:
                try:
                    await on_progress("\n".join(steps_log)[-1950:])
                except Exception:
                    log.debug("Progress update failed", exc_info=True)

        try:
            task_dir = self.scratch_root / str(thread_id)
            repo_dir = task_dir / "repo"
            if not repo_dir.exists():
                await emit("📥 Cloning repository workspace…")
                repo_dir, _ = await self.prepare_workspace(thread_id, repo_full, task_hint=task)
            await self._prepare_branch(repo_dir, branch)
            await self._discard_scratch_changes(repo_dir)
            base_sha = await self._git(repo_dir, "rev-parse", "HEAD")
            self._artifact(task_dir, "run.json", {
                "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                "repo": repo_full, "branch": branch, "base_sha": base_sha, "model": model,
                "context_tokens": getattr(self, "context_tokens", 16384),
                "output_tokens": getattr(self, "output_tokens", 4096),
                "task": task, "approved_plan": plan, "conventions": conventions,
                "documentation": ground_truth,
            })

            from .verification import load_protected_paths
            protected_paths = load_protected_paths(self.checks_file, repo_full, repo_dir)
            contract = ChangeContract.create(
                repo_dir, required_paths, allowed_paths, protected_paths=protected_paths,
            )
            target_files = select_target_files(repo_dir, f"{plan}\n{task}", max_files=8)
            target_files = contract.add_targets(target_files)
            log.info("Selected ranked target files for /code: %s", target_files)
            await emit(
                f"🎯 **Target files ({len(target_files)}):** "
                + ", ".join(f"`{f}`" for f in target_files)
            )

            live_go_doc = await extract_go_doc_context(repo_dir, target_files)
            combined_gt = "\n\n".join(p for p in (live_go_doc, ground_truth) if p.strip())
            if combined_gt:
                await emit("📚 **Ground-truth docs loaded** (pinned dependency APIs & indexed docs)")

            await emit("🧪 **Running clean baseline verification…**")
            baseline_report = await self._run_verification(repo_dir, repo_full)
            baseline_output, baseline_passed = baseline_report
            self._artifact(task_dir, "verification-baseline.txt", baseline_output)
            if hasattr(baseline_report, "as_dict"):
                self._artifact(task_dir, "verification-baseline.json", baseline_report.as_dict())
            if not baseline_passed:
                self._artifact(task_dir, "run-result.json", {
                    "status": "failed_baseline", "error": "Baseline verification failed",
                })
                return RunOutcome(
                    False, branch, "", "", baseline_output,
                    "Baseline verification failed before generation; repository or environment must be fixed first.",
                )

            await self._run_aider(
                repo_dir, target_files, task, plan, conventions, emit,
                ground_truth=combined_gt, model=model, on_think=on_think,
                allowed_paths=contract.allowed_paths,
            )
            status_out = await self._git(repo_dir, "status", "--porcelain")

            if not status_out.strip():
                self._artifact(task_dir, "run-result.json", {
                    "status": "failed_generation", "error": "No files were modified",
                })
                return RunOutcome(
                    ok=False,
                    branch=branch,
                    commit_sha="",
                    diff_stat="",
                    test_output="",
                    error="No files were modified during the edit pass.",
                )

            # Run verification, and if tests fail, attempt one self-healing repair pass
            await emit("🧪 **Running required component verification…**")
            test_report = await self._run_verification(repo_dir, repo_full)
            test_output, passed = test_report
            self._artifact(task_dir, "verification-generation.txt", test_output)
            if hasattr(test_report, "as_dict"):
                self._artifact(task_dir, "verification-generation.json", test_report.as_dict())
            if passed:
                await emit(f"✅ **Verification passed:** `{test_output.splitlines()[0]}`")
            elif target_files:
                await emit(
                    "⚠️ **Verification failed — running self-healing repair pass…**\n```\n"
                    + _verification_excerpt(test_report, test_output) + "\n```"
                )
                await self._repair_on_error(
                    repo_dir, target_files, task, plan, test_output, emit,
                    conventions=conventions, ground_truth=combined_gt, model=model,
                    allowed_paths=contract.allowed_paths,
                )
                test_report = await self._run_verification(repo_dir, repo_full)
                test_output, passed = test_report
                self._artifact(task_dir, "verification-repair.txt", test_output)
                if hasattr(test_report, "as_dict"):
                    self._artifact(task_dir, "verification-repair.json", test_report.as_dict())
                await emit(
                    f"✅ **Self-heal verification passed!**"
                    if passed
                    else "⚠️ **Post-repair verification:**\n```\n"
                    + _verification_excerpt(test_report, test_output) + "\n```"
                )

            if not passed:
                self._artifact(task_dir, "run-result.json", {"status": "failed_verification"})
                return RunOutcome(
                    False, branch, "", "", test_output,
                    "Verification failed; changes retained locally, nothing published.",
                    base_sha=base_sha,
                    verification=test_report.as_dict() if hasattr(test_report, "as_dict") else None,
                )

            contract_result = contract.evaluate(await self._changed_paths_since(repo_dir, base_sha))
            contract_artifact = {
                "base_sha": base_sha,
                "required_paths": list(contract.required_paths),
                "allowed_paths": list(contract.allowed_paths),
                "protected_paths": list(contract.protected_paths),
                **contract_result.as_dict(),
            }
            self._artifact(task_dir, "change-contract.json", contract_artifact)
            if not contract_result.ok:
                return RunOutcome(
                    False, branch, "", "", test_output,
                    "Change contract failed; nothing published: " + contract_result.failure_message(),
                    base_sha=base_sha,
                    verification=test_report.as_dict() if hasattr(test_report, "as_dict") else None,
                    change_contract=contract_artifact,
                )

            await emit("📦 **Committing and pushing branch to GitHub…**")
            self._artifact(task_dir, "proposed.diff", await self._git(repo_dir, "diff", "--no-ext-diff"))
            await self._git(repo_dir, "add", "-A")
            commit_msg = validate_commit_title(commit_title) if commit_title else f"agent: {task.splitlines()[0][:68]}"
            c_code, c_out = await _run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_dir)
            if c_code != 0:
                self._artifact(task_dir, "run-result.json", {
                    "status": "failed_commit", "error": self._redact(c_out),
                })
                return RunOutcome(
                    ok=False,
                    branch=branch,
                    commit_sha="",
                    diff_stat="",
                    test_output=test_output,
                    error=f"git commit failed: {self._redact(c_out)[:400]}",
                )

            sha_out = await self._git(repo_dir, "rev-parse", "--short", "HEAD")
            diff_stat = await self._git(repo_dir, "show", "--stat", "--oneline", "HEAD")
            cumulative_diff_stat = await self._cumulative_diff_stat(repo_dir)
            await self._assert_branch(repo_dir, branch)

            p_code, p_out = await _run_cmd(
                ["git", "push", "-u", "origin", branch],
                cwd=repo_dir,
                env=self._git_auth_env(),
                timeout=120,
            )
            if p_code != 0:
                self._artifact(task_dir, "run-result.json", {
                    "status": "failed_push", "commit": sha_out, "error": self._redact(p_out),
                })
                return RunOutcome(
                    ok=False,
                    branch=branch,
                    commit_sha=sha_out.strip(),
                    diff_stat=diff_stat.strip(),
                    test_output=test_output,
                    error=f"git push failed: {self._redact(p_out)[:400]}",
                )

            self._artifact(task_dir, "run-result.json", {
                "status": "published", "commit": sha_out, "verification_passed": True,
            })
            return RunOutcome(
                ok=True,
                branch=branch,
                commit_sha=sha_out.strip(),
                diff_stat=diff_stat.strip(),
                test_output=test_output,
                base_sha=base_sha,
                cumulative_diff_stat=cumulative_diff_stat,
                verification=test_report.as_dict() if hasattr(test_report, "as_dict") else None,
                change_contract=contract_artifact,
            )
        except asyncio.CancelledError:
            if "task_dir" in locals():
                self._artifact(task_dir, "run-result.json", {"status": "cancelled"})
            raise
        except Exception as exc:
            if "task_dir" in locals():
                self._artifact(task_dir, "run-result.json", {
                    "status": "error", "error_type": type(exc).__name__, "error": str(exc),
                })
            raise
        finally:
            self.active_status.pop(thread_id, None)

    def _clean_aider_artifacts(self, repo_dir: Path) -> None:
        for p in repo_dir.rglob(".aider*"):
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)

    async def _run_aider(
        self, repo_dir, target_files, task, plan, conventions, emit,
        ground_truth="", model="smart", on_think=None, allowed_paths=None,
    ):
        await emit("Running Aider repository map and diff editor (local-model limit: 15 minutes)…")
        scope_instruction = (
            "Writable file scope is unrestricted."
            if allowed_paths is None
            else "Writable file scope (hard enforced):\n"
            + "\n".join(f"- {path}" for path in allowed_paths)
            + "\nDo not create, edit, rename, or delete any other path."
        )
        payload = {
            "model": model,
            "artifact_dir": str(repo_dir.parent / "aider-cache"),
            "files": target_files,
            "allowed_files": None if allowed_paths is None else list(allowed_paths),
            "context_tokens": self.context_tokens,
            "output_tokens": self.output_tokens,
            "result_file": str(repo_dir.parent / "artifacts" / "aider-result.json"),
            "message": (
                f"Task:\n{task}\n\nApproved plan:\n{plan}\n\n"
                f"{scope_instruction}\n\n"
                f"{CODE_STYLE_CONVENTIONS}\nProject conventions:\n{conventions}\n\n"
                f"Reference documentation:\n{ground_truth}\n\n"
                "Inspect relevant implementations and tests. Complete the approved task across files."
            ),
        }
        env = {**os.environ, "OPENAI_API_BASE": self.llm_base_url,
               "OPENAI_API_KEY": self.llm_api_key, "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory(prefix="aider-", dir=repo_dir.parent) as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(json.dumps(payload), encoding="utf-8")
            self._artifact(repo_dir.parent, "aider-request.json", payload)
            code, output = await _run_cmd(
                [sys.executable, str(Path(__file__).with_name("aider_worker.py")), str(request)],
                cwd=repo_dir, env=env, timeout=AIDER_EDIT_TIMEOUT,
            )
        safe_output = self._redact(output).replace(self.llm_api_key, "***") if self.llm_api_key else self._redact(output)
        self._artifact(repo_dir.parent, "aider.log", safe_output)
        if code:
            raise RuntimeError(f"Aider failed (exit {code}); see {repo_dir.parent / 'artifacts' / 'aider.log'}")
        await self._record_runtime_context(repo_dir.parent)
        await emit("Aider edit pass finished; required verification follows.")

    async def _record_runtime_context(self, artifact_dir: Path) -> None:
        if not self.ollama_url or not self.ollama_model:
            return
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.ollama_url}/api/ps")
            response.raise_for_status()
            data = response.json()
        match = next((m for m in data.get("models", []) if m.get("name") == self.ollama_model), None)
        if not match:
            raise RuntimeError(f"Ollama did not report loaded model {self.ollama_model}")
        observed = int(match.get("context_length") or 0)
        self._artifact(artifact_dir, "ollama-runtime.json", match)
        if observed < self.context_tokens:
            raise RuntimeError(
                f"Ollama allocated {observed} context tokens; configured requirement is {self.context_tokens}"
            )

    def _sibling_context(self, repo_dir: Path, rel: str, snapshots: dict[str, str]) -> str:
        """Gather read-only context of other target files AND same-directory package files (e.g. Go package siblings)."""
        target_path = repo_dir / rel
        same_dir_files: dict[str, str] = {}
        if target_path.parent.is_dir():
            for sib in sorted(target_path.parent.iterdir()):
                if (
                    sib.is_file()
                    and sib != target_path
                    and sib.suffix.lower() in CODE_EXTS
                    and not sib.name.endswith(("_test.go", ".test.ts", ".test.tsx", ".spec.ts"))
                ):
                    sib_rel = str(sib.relative_to(repo_dir)).replace("\\", "/")
                    same_dir_files[sib_rel] = sib.read_text(encoding="utf-8", errors="replace")

        merged = {**same_dir_files, **snapshots}
        symbol_index = extract_package_symbols(merged, exclude_rel=rel)
        parts = [
            f"--- Sibling file in repo (read-only context, DO NOT redeclare its types): {other} ---\n{body[:3500]}"
            for other, body in merged.items()
            if other != rel
        ]
        if symbol_index:
            budget = max(2000, 11000 - len(symbol_index) - 2)
            return symbol_index + "\n\n" + "\n\n".join(parts)[:budget]
        return "\n\n".join(parts)[:11000]

    async def push_existing_branch(self, thread_id: int, repo_full: str, branch: str) -> RunOutcome:
        """Push an already-committed branch in the scratch workspace (used when retrying after a 403 token fix)."""
        repo_dir = self.scratch_root / str(thread_id) / "repo"
        if not repo_dir.exists():
            return RunOutcome(ok=False, branch=branch, commit_sha="", diff_stat="", test_output="", error="Scratch repo not found.")
        await self._assert_branch(repo_dir, branch)
        await self._git(repo_dir, "remote", "set-url", "origin", self._remote_url(repo_full))
        dirty = await self._git(repo_dir, "status", "--porcelain=v1", "-z")
        if dirty:
            return RunOutcome(
                False, branch, "", "", "",
                "Push retry requires a clean worktree; discard or commit unpublished changes first.",
            )
        verified_sha = await self._git(repo_dir, "rev-parse", "HEAD")
        sha_out = await self._git(repo_dir, "rev-parse", "--short", "HEAD")
        diff_stat = await self._git(repo_dir, "show", "--stat", "--oneline", "HEAD")
        cumulative_diff_stat = await self._cumulative_diff_stat(repo_dir)
        test_report = await self._run_verification(repo_dir, repo_full)
        test_output, passed = test_report
        if not passed:
            return RunOutcome(
                False, branch, sha_out.strip(), diff_stat.strip(), test_output,
                "Verification failed; existing commit was not pushed.",
                verification=test_report.as_dict() if hasattr(test_report, "as_dict") else None,
            )
        current_sha = await self._git(repo_dir, "rev-parse", "HEAD")
        if current_sha != verified_sha:
            return RunOutcome(
                False, branch, sha_out.strip(), diff_stat.strip(), test_output,
                "Branch HEAD changed after verification; refusing to push unverified commit.",
            )
        p_code, p_out = await _run_cmd(
            ["git", "push", "-u", "origin", branch],
            cwd=repo_dir,
            env=self._git_auth_env(),
            timeout=120,
        )
        if p_code != 0:
            return RunOutcome(
                ok=False,
                branch=branch,
                commit_sha=sha_out.strip(),
                diff_stat=diff_stat.strip(),
                test_output=test_output,
                error=f"git push failed: {self._redact(p_out)[:400]}",
            )
        return RunOutcome(
            ok=True,
            branch=branch,
            commit_sha=sha_out.strip(),
            diff_stat=diff_stat.strip(),
            test_output=test_output,
            cumulative_diff_stat=cumulative_diff_stat,
            verification=(
                {**test_report.as_dict(), "commit_sha": verified_sha}
                if hasattr(test_report, "as_dict") else {"commit_sha": verified_sha}
            ),
        )

    async def followup_fix(
        self,
        thread_id: int,
        repo_full: str,
        branch: str,
        task: str,
        plan: str,
        feedback: str,
        conventions: str = "",
        ground_truth: str = "",
        model: str = "smart",
        commit_title: str = "",
        required_paths: tuple[str, ...] | list[str] = (),
        allowed_paths: tuple[str, ...] | list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> RunOutcome:
        """Apply a targeted follow-up fix (e.g. from pasted CI/CD errors or review comments) and push to the existing branch."""
        steps_log: list[str] = [f"🔧 **Running Targeted Fix on `{repo_full}` → `{branch}`**"]

        async def emit(line: str) -> None:
            self.active_status[thread_id] = line
            steps_log.append(line)
            if on_progress:
                try:
                    await on_progress("\n".join(steps_log)[-1950:])
                except Exception:
                    log.debug("Progress update failed", exc_info=True)

        try:
            repo_dir = self.scratch_root / str(thread_id) / "repo"
            if not repo_dir.exists():
                await emit("📥 Re-cloning repository branch…")
                repo_dir, _ = await self.prepare_workspace(thread_id, repo_full, task_hint=task)
                await self._git(
                    repo_dir, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                    authenticated=True,
                )
                await self._git(repo_dir, "checkout", "-b", branch, f"origin/{branch}")

            await self._assert_branch(repo_dir, branch)
            await self._discard_scratch_changes(repo_dir)
            await self._git(repo_dir, "remote", "set-url", "origin", self._remote_url(repo_full))
            await self._sync_branch(repo_dir, branch)
            base_sha = await self._git(repo_dir, "rev-parse", "HEAD")
            from .verification import load_protected_paths
            protected_paths = load_protected_paths(self.checks_file, repo_full, repo_dir)
            contract = ChangeContract.create(
                repo_dir, required_paths, allowed_paths, protected_paths=protected_paths,
            )
            target_files = select_target_files(repo_dir, f"{feedback}\n{plan}\n{task}", max_files=8)
            target_files = contract.add_targets(target_files)
            await self._repair_on_error(
                repo_dir, target_files, task, plan, feedback, emit,
                conventions=conventions, ground_truth=ground_truth, model=model,
                allowed_paths=contract.allowed_paths,
            )

            await emit("🧪 **Running required component verification…**")
            test_report = await self._run_verification(repo_dir, repo_full)
            test_output, passed = test_report
            self._artifact(repo_dir.parent, "verification-fix.txt", test_output)
            if hasattr(test_report, "as_dict"):
                self._artifact(repo_dir.parent, "verification-fix.json", test_report.as_dict())
            if not passed:
                await emit(
                    "⚠️ **Verification still reported issues — running second repair pass…**\n```\n"
                    + _verification_excerpt(test_report, test_output) + "\n```"
                )
                await self._repair_on_error(
                    repo_dir, target_files, task, plan,
                    f"Original owner feedback:\n{feedback}\n\nVerification output:\n{test_output}", emit,
                    conventions=conventions, ground_truth=ground_truth, model=model,
                    allowed_paths=contract.allowed_paths,
                )
                test_report = await self._run_verification(repo_dir, repo_full)
                test_output, passed = test_report
                self._artifact(repo_dir.parent, "verification-fix-repair.txt", test_output)
                if hasattr(test_report, "as_dict"):
                    self._artifact(repo_dir.parent, "verification-fix-repair.json", test_report.as_dict())

            await emit(
                "✅ **Verification passed!**"
                if passed else "⚠️ **Verification output:**\n```\n"
                + _verification_excerpt(test_report, test_output) + "\n```"
            )

            if not passed:
                return RunOutcome(False, branch, "", "", test_output,
                                  "Verification failed; repair retained locally, nothing published.")

            contract_result = contract.evaluate(await self._changed_paths_since(repo_dir, base_sha))
            contract_artifact = {
                "base_sha": base_sha,
                "required_paths": list(contract.required_paths),
                "allowed_paths": list(contract.allowed_paths),
                "protected_paths": list(contract.protected_paths),
                **contract_result.as_dict(),
            }
            self._artifact(repo_dir.parent, "change-contract.json", contract_artifact)
            if not contract_result.ok:
                return RunOutcome(
                    False, branch, "", "", test_output,
                    "Change contract failed; nothing published: " + contract_result.failure_message(),
                    base_sha=base_sha,
                    verification=test_report.as_dict() if hasattr(test_report, "as_dict") else None,
                    change_contract=contract_artifact,
                )

            status_out = await self._git(repo_dir, "status", "--porcelain")
            if status_out.strip():
                await emit("📦 **Committing and pushing fix to GitHub…**")
                await self._git(repo_dir, "add", "-A")
                commit_msg = validate_commit_title(commit_title) if commit_title else "fix(repo): address verification feedback"
                await self._git(repo_dir, "commit", "-m", commit_msg)
            else:
                await emit("⚪ No additional file diffs produced.")

            outcome = await self.push_existing_branch(thread_id, repo_full, branch)
            return replace(outcome, base_sha=base_sha, change_contract=contract_artifact)
        finally:
            self.active_status.pop(thread_id, None)

    async def _repair_on_error(
        self, repo_dir, target_files, task, plan, error_output, emit,
        conventions="", ground_truth="", model="smart", allowed_paths=None,
    ):
        await self._run_aider(
            repo_dir, target_files,
            f"{task}\n\nRepair these verification failures / review findings:\n{error_output}",
            plan, conventions, emit, ground_truth=ground_truth, model=model,
            allowed_paths=allowed_paths,
        )

    async def _run_verification(self, repo_dir: Path, repo_full: str):
        from .verification import VerificationReport, verify

        before = await self._worktree_fingerprint(repo_dir)
        report = await verify(self.checks_file, repo_full, repo_dir, self.verifier.run)
        output, passed = report
        after = await self._worktree_fingerprint(repo_dir)
        if after != before:
            mutation = "Verification MUTATED the worktree; refusing to publish test/setup side effects."
            check = {
                "component": "worktree integrity", "argv": [], "status": "FAIL", "exit_code": None,
                "duration_seconds": 0.0, "output": mutation,
            }
            return VerificationReport(
                output + "\n" + mutation, False, (*getattr(report, "checks", ()), check)
            )
        return report
