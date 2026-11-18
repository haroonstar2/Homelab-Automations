"""/log: quick progress entries synced to Postgres, local archive, and today's Notion Journal page."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import header

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)


def append_journal_line(data_dir: Path, now: datetime, text: str) -> Path:
    """Local markdown archive: /data/archive/journal/YYYY/YYYY-MM-DD.md"""
    path = data_dir / "archive" / "journal" / f"{now:%Y}" / f"{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Journal {now:%Y-%m-%d}\n\n")
        f.write(f"- {now:%H:%M} {' '.join(text.split())}\n")
    return path


class Journal(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="log", description="Log progress, a decision, or a blocker to today's journal")
    @app_commands.describe(text="What you did / decided / are stuck on")
    async def log(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.defer(ephemeral=True)
        now = datetime.now(ZoneInfo(self.bot.cfg.timezone))
        await self.bot.db.add_journal_entry("log", text)
        await asyncio.to_thread(append_journal_line, self.bot.cfg.data_dir, now, text)

        notion_url = ""
        if self.bot.notion.enabled:
            try:
                _, notion_url = await self.bot.notion.append_to_journal(
                    now.date(),
                    f"- **{now:%H:%M}** — {text}",
                )
            except Exception:
                log.exception("Failed syncing /log entry to Notion")

        desc = text[:3800]
        if notion_url:
            desc += f"\n\n🔗 **Notion Journal:** {notion_url}"
        await interaction.followup.send(embed=header("log", when=now, description=desc), ephemeral=True)
