"""/status and /logs: health check of LiteLLM, Ollama GPU/CPU state, Postgres, Notion, GitHub, SearXNG, and recent logs."""

from __future__ import annotations

from collections import deque
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
import httpx

from ..embeds import header

if TYPE_CHECKING:
    from ..bot import DevBot

LOG_BUFFER: deque[str] = deque(maxlen=150)


class RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


def _read_host_stats() -> str:
    try:
        load1, load5, load15 = Path("/proc/loadavg").read_text().split()[:3]
        mem: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                mem[parts[0].rstrip(":")] = int(parts[1])
        total_gb = mem.get("MemTotal", 0) / (1024 * 1024)
        avail_gb = mem.get("MemAvailable", 0) / (1024 * 1024)
        used_gb = max(0.0, total_gb - avail_gb)
        return f"Load: `{load1} {load5} {load15}` · RAM: `{used_gb:.1f} / {total_gb:.1f} GiB`"
    except Exception as exc:
        return f"unavailable ({exc})"


async def _fetch_ollama_ps() -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://ollama:11434/api/ps")
            resp.raise_for_status()
            models = resp.json().get("models", [])
        if not models:
            return "⚪ no models currently loaded"
        lines: list[str] = []
        for m in models:
            name = m.get("name", "?")
            size_gb = m.get("size", 0) / (1024 ** 3)
            vram_gb = m.get("size_vram", 0) / (1024 ** 3)
            gpu_pct = int(round((vram_gb / size_gb) * 100)) if size_gb > 0 else 0
            cpu_pct = max(0, 100 - gpu_pct)
            lines.append(f"• `{name}` — `{size_gb:.1f} GB` (`{cpu_pct}% CPU / {gpu_pct}% GPU`)")
        return "\n".join(lines)
    except Exception as exc:
        return f"❌ `{type(exc).__name__}: {exc}`"


class Status(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="status", description="Check devbot's model proxy, database, Notion, GitHub & SearXNG")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = header("status")

        try:
            models = await self.bot.llm.models()
            embed.add_field(name="LiteLLM", value="✅ " + ", ".join(f"`{m}`" for m in models), inline=False)
        except Exception as exc:
            embed.add_field(name="LiteLLM", value=f"❌ `{type(exc).__name__}: {exc}`"[:1024], inline=False)

        try:
            ok = await self.bot.db.ping()
            embed.add_field(name="Postgres (pgvector)", value="✅ connected" if ok else "❌ unexpected reply", inline=False)
        except Exception as exc:
            embed.add_field(name="Postgres (pgvector)", value=f"❌ `{type(exc).__name__}: {exc}`"[:1024], inline=False)

        if self.bot.notion.enabled:
            try:
                info = await self.bot.notion.ping()
                embed.add_field(name="Notion", value=f"✅ {info}", inline=False)
            except Exception as exc:
                embed.add_field(name="Notion", value=f"❌ `{type(exc).__name__}: {exc}`"[:1024], inline=False)
        else:
            embed.add_field(name="Notion", value="⚪ not configured", inline=False)

        if self.bot.github.enabled:
            try:
                user = await self.bot.github.username()
                embed.add_field(name="GitHub", value=f"✅ authenticated as `{user}`", inline=False)
            except Exception as exc:
                embed.add_field(name="GitHub", value=f"❌ `{type(exc).__name__}: {exc}`"[:1024], inline=False)
        else:
            embed.add_field(name="GitHub", value="⚪ not configured", inline=False)

        try:
            hits = await self.bot.searxng.search("test", limit=1)
            embed.add_field(name="SearXNG", value=f"✅ reachable ({len(hits)} sample hit)", inline=False)
        except Exception as exc:
            embed.add_field(name="SearXNG", value=f"❌ `{type(exc).__name__}: {exc}`"[:1024], inline=False)

        active = self.bot.runner.active_status
        if active:
            lines = [f"• Thread `<#{tid}>`: {step}" for tid, step in active.items()]
            embed.add_field(name="Active Code Tasks", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Active Code Tasks", value="⚪ idle", inline=False)

        embed.add_field(name="Ollama Loaded Models", value=(await _fetch_ollama_ps())[:1024], inline=False)
        embed.add_field(name="Server Resources", value=_read_host_stats(), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="logs", description="Show recent devbot logs right in Discord")
    @app_commands.describe(lines="Number of recent log lines to show (default: 20, max: 50)")
    async def logs(self, interaction: discord.Interaction, lines: int = 20) -> None:
        n = max(1, min(lines, 50))
        recent = list(LOG_BUFFER)[-n:]
        body = "\n".join(recent) if recent else "(no logs captured yet)"
        if len(body) > 1900:
            body = body[-1900:]
        await interaction.response.send_message(f"```text\n{body}\n```", ephemeral=True)
