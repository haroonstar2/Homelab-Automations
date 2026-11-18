"""/ask: architecture & implementation Q&A in a Discord thread."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import header
from ..llm import SMART
from ..prompts import ASK_SYSTEM
from ..textutil import Turn, build_messages, split_message

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)

PLACEHOLDER = "⏳ Thinking… (local model on CPU, this can take a few minutes)"
ERROR_PREFIX = "⚠️"
TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".cs", ".rb", ".php", ".sh", ".sql", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".html", ".css", ".xml", ".log", ".env.example", ".dockerfile",
}
MAX_ATTACHMENT_CHARS = 20_000


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def _message_text(message: discord.Message) -> str:
    """Message content plus any text attachments (pasted docs, source files)."""
    parts = [message.content] if message.content else []
    for att in message.attachments:
        name = att.filename.lower()
        is_text = (att.content_type or "").startswith("text/") or any(name.endswith(e) for e in TEXT_EXTENSIONS)
        if not is_text:
            continue
        raw = (await att.read()).decode("utf-8", errors="replace")
        if len(raw) > MAX_ATTACHMENT_CHARS:
            raw = raw[:MAX_ATTACHMENT_CHARS] + "\n[... truncated ...]"
        parts.append(f"--- attachment: {att.filename} ---\n{raw}")
    return "\n\n".join(parts)


class Ask(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="ask", description="Ask an architecture / implementation question (opens a thread)")
    @app_commands.describe(
        question="Your question. Follow up, paste docs or attach files inside the thread.",
        project="Optional project name to pull conventions from Notion",
    )
    async def ask(self, interaction: discord.Interaction, question: str, project: str = "") -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /ask in a text channel, not a thread or DM.", ephemeral=True)
            return

        sub = f"{project}: {_short(question, 65)}" if project else _short(question, 80)
        embed = header("ask", subtitle=sub, description=question[:4000])
        await interaction.response.send_message(embed=embed)
        starter = await interaction.original_response()
        thread = await starter.create_thread(name=_short(question, 95), auto_archive_duration=1440)
        await self.bot.db.add_thread(thread.id, "ask", question, meta={"project": project})
        await self._answer(thread)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.bot.is_owner_message(message) or not isinstance(message.channel, discord.Thread):
            return
        record = await self.bot.db.get_thread(message.channel.id)
        if record is None or record["kind"] != "ask":
            return
        await self._answer(message.channel)

    async def _collect_turns(self, thread: discord.Thread) -> list[Turn]:
        bot_id = self.bot.user.id if self.bot.user else 0
        turns: list[Turn] = []
        async for msg in thread.history(limit=100, oldest_first=True):
            if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                continue
            if msg.author.id == bot_id:
                if msg.content.startswith((PLACEHOLDER[:1], ERROR_PREFIX)) or not msg.content:
                    continue
                turns.append(Turn("assistant", msg.content))
            elif msg.author.id == self.bot.cfg.owner_id:
                turns.append(Turn("user", await _message_text(msg)))
        return turns

    async def _answer(self, thread: discord.Thread) -> None:
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_thread(thread.id)
            if record is None:
                return
            meta = json.loads(record["meta"] or "{}") if isinstance(record["meta"], str) else (record["meta"] or {})
            project = meta.get("project", "")
            system_prompt = ASK_SYSTEM

            turns = await self._collect_turns(thread)
            combined_query = record["seed"] + "\n" + "\n".join(t.content for t in turns if t.role == "user")

            # Automatically detect and inspect any referenced GitHub repository
            matched_repo: str | None = None
            if self.bot.github.enabled:
                try:
                    matched_repo = await self.bot.github.find_repo_in_text(combined_query, explicit_project=project)
                    if matched_repo:
                        repo_snapshot = await self.bot.runner.inspect_repo(matched_repo, combined_query)
                        system_prompt += (
                            f"\n\n=== LIVE GITHUB REPOSITORY SNAPSHOT: {matched_repo} ===\n"
                            f"{repo_snapshot}\n"
                            f"=== END REPOSITORY SNAPSHOT ==="
                        )
                    else:
                        repos = await self.bot.github.list_all_user_repos()
                        names = ", ".join(r.get("name", "") for r in repos[:30] if r.get("name"))
                        if names:
                            system_prompt += f"\n\nUser's GitHub repositories: {names}"
                except Exception:
                    log.exception("Failed inspecting GitHub repository for /ask")

            target_proj = matched_repo or project
            if target_proj and self.bot.notion.enabled:
                try:
                    conv = await self.bot.notion.get_project_conventions(target_proj)
                    if conv:
                        system_prompt += f"\n\nProject Conventions ({target_proj}):\n{conv[:4000]}"
                except Exception:
                    log.warning("Could not load project conventions for %s", target_proj)

            messages = build_messages(system_prompt, record["seed"], turns, budget_chars=36_000)

            placeholder = await thread.send(PLACEHOLDER)
            try:
                async with thread.typing():
                    reply = await self.bot.llm.chat(SMART, messages)
            except Exception as exc:  # surface model/proxy failures in Discord
                log.exception("ask failed")
                await placeholder.edit(content=f"{ERROR_PREFIX} Model call failed: `{type(exc).__name__}: {exc}`"[:2000])
                return

            chunks = split_message(reply) or ["(the model returned an empty response)"]
            await placeholder.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await thread.send(chunk)
