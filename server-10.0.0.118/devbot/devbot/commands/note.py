"""/note: capture raw tech research in a thread, then synthesize & save to Notion."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import header
from ..llm import SMART
from ..prompts import NOTE_SYNTHESIS_SYSTEM
from ..textutil import split_message
from .ask import _message_text, _short

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)


class NoteSaveView(discord.ui.View):
    """Persistent button at the top of a /note thread to synthesize and save to Notion."""

    def __init__(self, cog: NoteCog, topic: str, project: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.topic = topic
        self.project = project

    @discord.ui.button(label="Synthesize & Save to Notion", style=discord.ButtonStyle.success, emoji="📓")
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.cog.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can save notes.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use this inside the note thread.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self.cog.synthesize_and_save(interaction.channel, self.topic, self.project, followup=interaction.followup)


class NoteCog(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="note", description="Start a tech note thread (drop raw notes/links, then save to Notion)")
    @app_commands.describe(
        topic="Technology or topic name (e.g., 'Redis Streams vs Kafka')",
        project="Optional project name to tag the note with",
    )
    async def note(self, interaction: discord.Interaction, topic: str, project: str = "") -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /note in a text channel.", ephemeral=True)
            return

        desc = (
            f"**Topic:** {topic}" + (f" · **Project:** `{project}`" if project else "") + "\n"
            "Drop your raw notes, code snippets, links, or questions in the thread below.\n"
            "When ready, click **Synthesize & Save to Notion** or run `/note_save` inside the thread."
        )
        embed = header("note", subtitle=_short(topic, 80), description=desc)
        await interaction.response.send_message(embed=embed)
        starter = await interaction.original_response()
        thread = await starter.create_thread(name=f"Note: {_short(topic, 85)}", auto_archive_duration=1440)
        await self.bot.db.add_thread(thread.id, "note", topic, meta={"project": project})
        view = NoteSaveView(self, topic, project)
        await thread.send(
            "Paste your scratch notes below. When finished, click the button or type `/note_save`.",
            view=view,
        )

    @app_commands.command(name="note_save", description="Synthesize the current /note thread and save it to Notion")
    async def note_save(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Run `/note_save` inside a `/note` thread.", ephemeral=True)
            return
        record = await self.bot.db.get_thread(interaction.channel.id)
        if not record or record["kind"] != "note":
            await interaction.response.send_message("This thread is not a `/note` thread.", ephemeral=True)
            return
        import json

        meta = json.loads(record["meta"] or "{}") if isinstance(record["meta"], str) else (record["meta"] or {})
        await interaction.response.defer(thinking=True)
        await self.synthesize_and_save(
            interaction.channel,
            record["seed"],
            meta.get("project", ""),
            followup=interaction.followup,
        )

    @app_commands.command(name="conventions", description="Save or append coding conventions for a project in Notion")
    @app_commands.describe(
        project="Project/repo name (e.g., 'IDKServer')",
        rules="Markdown rules/conventions for /ask and /code to follow",
        repo_url="Optional GitHub repo URL",
    )
    async def conventions(self, interaction: discord.Interaction, project: str, rules: str, repo_url: str = "") -> None:
        if not self.bot.notion.enabled:
            await interaction.response.send_message("⚠️ Notion is not configured (`NOTION_TOKEN`).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = await self.bot.notion.set_project_conventions(project, repo_url, rules)
        await interaction.followup.send(
            f"✅ Saved conventions for `{project}` in Notion: {url}",
            ephemeral=True,
        )

    async def synthesize_and_save(
        self,
        thread: discord.Thread,
        topic: str,
        project: str,
        followup: discord.Webhook | None = None,
    ) -> None:
        async with self.bot.lock_for(thread.id):
            raw_parts: list[str] = []
            async for msg in thread.history(limit=100, oldest_first=True):
                if msg.author.id == self.bot.cfg.owner_id:
                    txt = await _message_text(msg)
                    if txt.strip():
                        raw_parts.append(txt.strip())

            if not raw_parts:
                msg_text = "No notes found in this thread yet—drop some notes first!"
                if followup:
                    await followup.send(msg_text, ephemeral=True)
                else:
                    await thread.send(msg_text)
                return

            combined = "\n\n---\n\n".join(raw_parts)
            prompt = f"Topic: {topic}\n" + (f"Project: {project}\n\n" if project else "\n") + f"Raw notes:\n{combined}"
            messages = [
                {"role": "system", "content": NOTE_SYNTHESIS_SYSTEM},
                {"role": "user", "content": prompt[:24000]},
            ]
            synthesized = await self.bot.llm.chat(SMART, messages, max_tokens=2000)

            notion_url = ""
            if self.bot.notion.enabled:
                try:
                    _, notion_url = await self.bot.notion.create_note(
                        title=topic,
                        note_type="Tech",
                        markdown_body=synthesized,
                        project=project,
                        when=date.today(),
                    )
                except Exception as exc:
                    log.exception("Failed saving note to Notion")
                    notion_url = f"(Notion upload failed: `{exc}`)"

            header_line = f"✅ **Saved Tech Note: {topic}**"
            if notion_url:
                header_line += f"\n🔗 **Notion:** {notion_url}"
            full_out = f"{header_line}\n\n{synthesized}"
            chunks = split_message(full_out)
            if followup and chunks:
                await followup.send(chunks[0])
                for c in chunks[1:]:
                    await thread.send(c)
            else:
                for c in chunks:
                    await thread.send(c)
