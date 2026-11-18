"""Notion integration: Markdown <-> Notion blocks, Notes & Projects databases, nightly backup.

Uses the Notion REST API directly via httpx so we have exact control over block batching,
2000-character text splitting, and database schema creation.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_TEXT_LEN = 1900  # Notion enforces 2000 chars per rich_text item
MAX_BLOCKS_PER_CALL = 95  # Notion enforces 100 blocks per append call

# Notion's supported code block languages (subset + fallback to "plain text")
NOTION_LANGUAGES = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#", "css",
    "dart", "diff", "docker", "dockerfile", "elixir", "elm", "erlang", "flow", "fortran", "f#",
    "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "html", "java", "javascript",
    "json", "julia", "kotlin", "latex", "less", "lisp", "livescript", "lua", "makefile",
    "markdown", "markup", "matlab", "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl",
    "php", "plain text", "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby",
    "rust", "sass", "scala", "scheme", "scss", "shell", "sql", "swift", "toml", "typescript",
    "vb.net", "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
}
LANG_ALIASES = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "jsx": "javascript",
    "sh": "shell",
    "zsh": "shell",
    "yml": "yaml",
    "dockerfile": "docker",
    "cs": "c#",
    "cpp": "c++",
    "rs": "rust",
    "md": "markdown",
    "ps1": "powershell",
}


def normalize_language(lang: str) -> str:
    cleaned = lang.strip().lower()
    cleaned = LANG_ALIASES.get(cleaned, cleaned)
    return cleaned if cleaned in NOTION_LANGUAGES else "plain text"


def _rich_text(text: str) -> list[dict[str, Any]]:
    """Split text into <=1900-char Notion rich_text objects."""
    if not text:
        return [{"type": "text", "text": {"content": ""}}]
    chunks = [text[i : i + MAX_TEXT_LEN] for i in range(0, len(text), MAX_TEXT_LEN)]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def markdown_to_blocks(md: str) -> list[dict[str, Any]]:
    """Convert markdown into Notion block objects.

    Supports headings (#, ##, ###), fenced code blocks (```lang), bullet lists (- / *),
    numbered lists (1.), quotes (>), dividers (---), and paragraphs.
    """
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            lang = normalize_language(stripped[3:])
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_body = "\n".join(code_lines)
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {"rich_text": _rich_text(code_body), "language": lang},
            })
            continue

        # Divider
        if re.match(r"^[-*_]{3,}$", stripped):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # Headings
        if stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(stripped[4:].strip())},
            })
            i += 1
            continue
        if stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(stripped[3:].strip())},
            })
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": _rich_text(stripped[2:].strip())},
            })
            i += 1
            continue

        # Bullet list
        if re.match(r"^[-*+]\s+", stripped):
            item_text = re.sub(r"^[-*+]\s+", "", stripped)
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(item_text)},
            })
            i += 1
            continue

        # Numbered list
        if re.match(r"^\d+\.\s+", stripped):
            item_text = re.sub(r"^\d+\.\s+", "", stripped)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(item_text)},
            })
            i += 1
            continue

        # Quote
        if stripped.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": _rich_text(stripped[2:].strip())},
            })
            i += 1
            continue

        # Paragraph (collect consecutive plain lines)
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith(("```", "#", "- ", "* ", "+ ", "> "))
                or re.match(r"^\d+\.\s+", nxt)
                or re.match(r"^[-*_]{3,}$", nxt)
            ):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(" ".join(para_lines))},
        })

    return blocks


def blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Convert Notion block objects back into plain Markdown."""
    out: list[str] = []
    for b in blocks:
        btype = b.get("type", "")
        payload = b.get(btype, {}) or {}
        rich = payload.get("rich_text", [])
        text = "".join(r.get("plain_text", "") or r.get("text", {}).get("content", "") for r in rich)

        if btype == "heading_1":
            out.append(f"# {text}")
        elif btype == "heading_2":
            out.append(f"## {text}")
        elif btype == "heading_3":
            out.append(f"### {text}")
        elif btype == "bulleted_list_item":
            out.append(f"- {text}")
        elif btype == "numbered_list_item":
            out.append(f"1. {text}")
        elif btype == "quote":
            out.append(f"> {text}")
        elif btype == "code":
            lang = payload.get("language", "")
            lang = "" if lang == "plain text" else lang
            out.append(f"```{lang}\n{text}\n```")
        elif btype == "divider":
            out.append("---")
        elif text:
            out.append(text)
    return "\n\n".join(out)


class NotionClient:
    def __init__(self, token: str, root_page_id: str) -> None:
        import httpx

        self.token = token.strip()
        self.root_page_id = root_page_id.strip().replace("-", "")
        self._http = httpx.AsyncClient(
            base_url=NOTION_API,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.notes_db_id: str | None = None
        self.projects_db_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.root_page_id)

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._http.request(method, path, json=json_body)
        if resp.status_code >= 400:
            raise RuntimeError(f"Notion API {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def ensure_databases(self) -> tuple[str, str]:
        """Find or create the 'Notes' and 'Projects' databases inside root_page_id."""
        if self.notes_db_id and self.projects_db_id:
            return self.notes_db_id, self.projects_db_id

        try:
            children = await self._req("GET", f"/blocks/{self.root_page_id}/children?page_size=100")
        except RuntimeError as exc:
            if "404" in str(exc):
                # Fallback: check if another top-level page was shared with the integration
                search = await self._req(
                    "POST",
                    "/search",
                    {"filter": {"value": "page", "property": "object"}, "page_size": 20},
                )
                pages = search.get("results", [])
                if pages:
                    fallback_id = pages[0]["id"].replace("-", "")
                    log.info("Configured root page not found; falling back to shared page %s", fallback_id)
                    self.root_page_id = fallback_id
                    children = await self._req("GET", f"/blocks/{self.root_page_id}/children?page_size=100")
                else:
                    raise
            else:
                raise
        for block in children.get("results", []):
            if block.get("type") == "child_database":
                title = (block.get("child_database") or {}).get("title", "")
                if title == "Notes":
                    self.notes_db_id = block["id"]
                elif title == "Projects":
                    self.projects_db_id = block["id"]

        if not self.notes_db_id:
            log.info("Creating Notion 'Notes' database under %s", self.root_page_id)
            created = await self._req(
                "POST",
                "/databases",
                {
                    "parent": {"type": "page_id", "page_id": self.root_page_id},
                    "title": [{"type": "text", "text": {"content": "Notes"}}],
                    "properties": {
                        "Title": {"title": {}},
                        "Type": {
                            "select": {
                                "options": [
                                    {"name": "Tech", "color": "blue"},
                                    {"name": "Journal", "color": "yellow"},
                                    {"name": "Retro", "color": "green"},
                                    {"name": "Research", "color": "purple"},
                                ]
                            }
                        },
                        "Project": {"rich_text": {}},
                        "Tags": {"multi_select": {}},
                        "Date": {"date": {}},
                    },
                },
            )
            self.notes_db_id = created["id"]

        if not self.projects_db_id:
            log.info("Creating Notion 'Projects' database under %s", self.root_page_id)
            created = await self._req(
                "POST",
                "/databases",
                {
                    "parent": {"type": "page_id", "page_id": self.root_page_id},
                    "title": [{"type": "text", "text": {"content": "Projects"}}],
                    "properties": {
                        "Name": {"title": {}},
                        "Repo": {"url": {}},
                    },
                },
            )
            self.projects_db_id = created["id"]

        return self.notes_db_id, self.projects_db_id

    async def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> None:
        for i in range(0, len(blocks), MAX_BLOCKS_PER_CALL):
            batch = blocks[i : i + MAX_BLOCKS_PER_CALL]
            await self._req("PATCH", f"/blocks/{page_id}/children", {"children": batch})

    async def get_page_markdown(self, page_id: str) -> str:
        blocks: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = await self._req("GET", path)
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return blocks_to_markdown(blocks)

    async def create_note(
        self,
        title: str,
        note_type: str,
        markdown_body: str,
        project: str = "",
        tags: list[str] | None = None,
        when: date | None = None,
    ) -> tuple[str, str]:
        """Create a page in the Notes database and return (page_id, url)."""
        notes_db, _ = await self.ensure_databases()
        when = when or date.today()
        blocks = markdown_to_blocks(markdown_body)
        first_batch = blocks[:MAX_BLOCKS_PER_CALL]
        rest = blocks[MAX_BLOCKS_PER_CALL:]

        props: dict[str, Any] = {
            "Title": {"title": [{"type": "text", "text": {"content": title[:200]}}]},
            "Type": {"select": {"name": note_type}},
            "Date": {"date": {"start": when.isoformat()}},
        }
        if project:
            props["Project"] = {"rich_text": [{"type": "text", "text": {"content": project[:200]}}]}
        if tags:
            props["Tags"] = {"multi_select": [{"name": t[:50]} for t in tags[:10]]}

        page = await self._req(
            "POST",
            "/pages",
            {
                "parent": {"database_id": notes_db},
                "properties": props,
                "children": first_batch,
            },
        )
        page_id = page["id"]
        if rest:
            await self.append_blocks(page_id, rest)
        return page_id, page.get("url", "")

    async def append_to_journal(self, day: date, markdown_section: str) -> tuple[str, str]:
        """Find or create today's Journal page ('Journal YYYY-MM-DD') and append blocks."""
        notes_db, _ = await self.ensure_databases()
        title = f"Journal {day.isoformat()}"
        query = await self._req(
            "POST",
            f"/databases/{notes_db}/query",
            {
                "filter": {
                    "and": [
                        {"property": "Title", "title": {"equals": title}},
                        {"property": "Type", "select": {"equals": "Journal"}},
                    ]
                },
                "page_size": 1,
            },
        )
        results = query.get("results", [])
        if results:
            page = results[0]
            page_id = page["id"]
            url = page.get("url", "")
            await self.append_blocks(page_id, markdown_to_blocks(markdown_section))
            return page_id, url
        return await self.create_note(title=title, note_type="Journal", markdown_body=markdown_section, when=day)

    async def _get_single_project_conventions(self, projects_db: str, name: str) -> str:
        query = await self._req(
            "POST",
            f"/databases/{projects_db}/query",
            {
                "filter": {"property": "Name", "title": {"contains": name}},
                "page_size": 1,
            },
        )
        results = query.get("results", [])
        if not results:
            return ""
        return await self.get_page_markdown(results[0]["id"])

    async def get_project_conventions(self, project_name: str) -> str:
        """Read global ('default') + project-specific conventions markdown from the Projects database."""
        if not self.enabled:
            return ""
        _, projects_db = await self.ensure_databases()
        parts: list[str] = []

        default_conv = await self._get_single_project_conventions(projects_db, "default")
        if default_conv.strip():
            parts.append(default_conv.strip())

        short_name = project_name.split("/")[-1].strip()
        if short_name and short_name.lower() != "default":
            proj_conv = await self._get_single_project_conventions(projects_db, short_name)
            if proj_conv.strip():
                parts.append(proj_conv.strip())

        return "\n\n---\n\n".join(parts)

    async def set_project_conventions(self, project_name: str, repo_url: str, markdown_body: str) -> str:
        """Create or append to a project's conventions page in the Projects database."""
        _, projects_db = await self.ensure_databases()
        short_name = project_name.split("/")[-1]
        query = await self._req(
            "POST",
            f"/databases/{projects_db}/query",
            {"filter": {"property": "Name", "title": {"equals": short_name}}, "page_size": 1},
        )
        results = query.get("results", [])
        blocks = markdown_to_blocks(markdown_body)
        if results:
            page_id = results[0]["id"]
            await self.append_blocks(page_id, blocks)
            return results[0].get("url", "")
        props: dict[str, Any] = {
            "Name": {"title": [{"type": "text", "text": {"content": short_name}}]},
        }
        if repo_url:
            props["Repo"] = {"url": repo_url}
        page = await self._req(
            "POST",
            "/pages",
            {
                "parent": {"database_id": projects_db},
                "properties": props,
                "children": blocks[:MAX_BLOCKS_PER_CALL],
            },
        )
        if len(blocks) > MAX_BLOCKS_PER_CALL:
            await self.append_blocks(page["id"], blocks[MAX_BLOCKS_PER_CALL:])
        return page.get("url", "")

    async def backup_all_to_disk(self, backup_dir: Path) -> int:
        """Export all pages in the Notes and Projects databases to local markdown files."""
        notes_db, projects_db = await self.ensure_databases()
        count = 0
        for db_label, db_id, title_prop in [("notes", notes_db, "Title"), ("projects", projects_db, "Name")]:
            target_dir = backup_dir / db_label
            target_dir.mkdir(parents=True, exist_ok=True)
            data = await self._req("POST", f"/databases/{db_id}/query", {"page_size": 100})
            for page in data.get("results", []):
                props = page.get("properties", {})
                title_items = (props.get(title_prop) or {}).get("title", [])
                raw_title = "".join(t.get("plain_text", "") for t in title_items) or page["id"]
                safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_title).strip("-")[:80] or page["id"]
                md = await self.get_page_markdown(page["id"])
                out_file = target_dir / f"{safe_name}.md"
                out_file.write_text(f"# {raw_title}\n\n{md}\n", encoding="utf-8")
                count += 1
        return count

    async def ping(self) -> str:
        notes_db, projects_db = await self.ensure_databases()
        return f"Notes DB `{notes_db[:8]}…`, Projects DB `{projects_db[:8]}…`"
