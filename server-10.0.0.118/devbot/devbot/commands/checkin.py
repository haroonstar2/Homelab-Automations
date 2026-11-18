"""Scheduled & on-demand check-ins, end-of-day wrap-ups, weekly retros, /resume, and SearXNG research spikes."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..embeds import header
from ..llm import FAST, SMART
from ..prompts import RESEARCH_SPIKE_SYSTEM, RETRO_SYSTEM, WRAPUP_SYSTEM
from ..textutil import split_message
from .journal import append_journal_line

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)

BLOCKER_RE = re.compile(r"\b(blocker|blocked|stuck\s+on|failing\s+to|error\s+with)\b", re.IGNORECASE)


class CheckinCog(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot
        self._tz = ZoneInfo(bot.cfg.timezone)
        self.morning_loop.change_interval(time=time(hour=8, minute=0, tzinfo=self._tz))
        self.evening_loop.change_interval(time=time(hour=18, minute=0, tzinfo=self._tz))
        self.backup_loop.change_interval(time=time(hour=2, minute=0, tzinfo=self._tz))

    async def cog_load(self) -> None:
        self.morning_loop.start()
        self.evening_loop.start()
        self.backup_loop.start()

    async def cog_unload(self) -> None:
        self.morning_loop.cancel()
        self.evening_loop.cancel()
        self.backup_loop.cancel()

    async def _general_channel(self) -> discord.TextChannel | None:
        ch = self.bot.get_channel(self.bot.cfg.channel_id)
        if ch is None:
            ch = await self.bot.fetch_channel(self.bot.cfg.channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    # --- Scheduled tasks -------------------------------------------------

    @tasks.loop(time=time(hour=8, minute=0))
    async def morning_loop(self) -> None:
        ch = await self._general_channel()
        if ch:
            await self.post_morning_checkin(ch)

    @tasks.loop(time=time(hour=18, minute=0))
    async def evening_loop(self) -> None:
        ch = await self._general_channel()
        if not ch:
            return
        now = datetime.now(self._tz)
        await self.post_evening_wrapup(ch)
        if now.weekday() == 6:  # Sunday 6:00 PM -> also post Weekly Retro
            await self.post_weekly_retro(ch)

    @tasks.loop(time=time(hour=2, minute=0))
    async def backup_loop(self) -> None:
        if not self.bot.notion.enabled:
            return
        try:
            backup_dir = self.bot.cfg.data_dir / "backup"
            count = await self.bot.notion.backup_all_to_disk(backup_dir)
            log.info("Nightly Notion backup exported %d pages to %s", count, backup_dir)
        except Exception:
            log.exception("Nightly Notion backup failed")

    @morning_loop.before_loop
    @evening_loop.before_loop
    @backup_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # --- Slash commands --------------------------------------------------

    @app_commands.command(name="checkin", description="Post the Git-aware morning check-in right now")
    async def checkin_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run `/checkin` in a text channel.", ephemeral=True)
            return
        await interaction.response.send_message("Posting morning check-in…", ephemeral=True)
        await self.post_morning_checkin(interaction.channel)

    @app_commands.command(name="wrapup", description="Post the end-of-day wrap-up right now")
    async def wrapup_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run `/wrapup` in a text channel.", ephemeral=True)
            return
        await interaction.response.send_message("Building end-of-day wrap-up…", ephemeral=True)
        await self.post_evening_wrapup(interaction.channel)

    @app_commands.command(name="retro", description="Generate and post the weekly engineering retro right now")
    async def retro_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run `/retro` in a text channel.", ephemeral=True)
            return
        await interaction.response.send_message("Generating weekly retro…", ephemeral=True)
        await self.post_weekly_retro(interaction.channel)

    @app_commands.command(name="resume", description="Where did I leave off? Shows last handoff, commits, PRs & blockers")
    @app_commands.describe(repo="Optional repository name to filter GitHub activity")
    async def resume(self, interaction: discord.Interaction, repo: str = "") -> None:
        await interaction.response.defer()
        now = datetime.now(self._tz)
        commits, prs, agent_branches = await self.bot.github.recent_activity(hours=72, repo_filter=repo or None)
        handoff = await self.bot.db.get_latest_journal_entry("handoff")
        plan = await self.bot.db.get_latest_journal_entry("plan")
        blocker = await self.bot.db.get_latest_journal_entry("blocker")

        sections: list[str] = []
        if handoff:
            sections.append(f"**Last Handoff ({handoff['created_at'].astimezone(self._tz):%b %d %H:%M}):**\n{handoff['body']}")
        if plan:
            sections.append(f"**Latest Plan ({plan['created_at'].astimezone(self._tz):%b %d %H:%M}):**\n{plan['body']}")
        if blocker:
            sections.append(f"**Last Logged Blocker:**\n{blocker['body']}")

        if commits:
            c_lines = [f"- `{c.repo}` [`{c.sha}`]({c.url}) {c.message}" for c in commits[:6]]
            sections.append("**Recent Commits (72h):**\n" + "\n".join(c_lines))
        else:
            sections.append("**Recent Commits (72h):** None found.")

        if prs:
            p_lines = [f"- `{p.repo}#{p.number}` [{p.title}]({p.url}) (`{p.branch}`)" for p in prs[:5]]
            sections.append("**Open PRs:**\n" + "\n".join(p_lines))

        if agent_branches:
            sections.append("**Active `agent/*` Branches:** " + ", ".join(f"`{b}`" for b in agent_branches[:8]))

        embed = header("resume", subtitle=repo or "All Projects", description="\n\n".join(sections)[:4000], when=now)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="research", description="Run a SearXNG web research spike and save findings to Notion")
    @app_commands.describe(query="Blocker or technical question to research")
    async def research(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        report, notion_url = await self.run_research_spike(query)
        desc = f"**Query:** {query}" + (f"\n🔗 **Notion:** {notion_url}" if notion_url else "")
        embed = header("research", subtitle=query[:70], description=desc)
        starter = await interaction.followup.send(embed=embed, wait=True)
        if isinstance(interaction.channel, discord.TextChannel):
            thread = await interaction.channel.create_thread(
                name=f"Research: {query[:80]}",
                message=starter,
                auto_archive_duration=1440,
            )
            for chunk in split_message(report):
                await thread.send(chunk)
        else:
            for chunk in split_message(report):
                await interaction.followup.send(chunk)

    # --- Core implementations --------------------------------------------

    async def post_morning_checkin(self, channel: discord.TextChannel) -> None:
        now = datetime.now(self._tz)
        commits, prs, agent_branches = await self.bot.github.recent_activity(hours=24)
        handoff = await self.bot.db.get_latest_journal_entry("handoff")

        lines: list[str] = ["**Yesterday on GitHub**"]
        if commits:
            by_repo: dict[str, list[str]] = {}
            for c in commits:
                by_repo.setdefault(c.repo, []).append(c.message)
            for repo_name, msgs in list(by_repo.items())[:4]:
                preview = "; ".join(msgs[:2])
                lines.append(f"• `{repo_name}`: {len(msgs)} commit(s) ({preview[:90]})")
        else:
            lines.append("• No commits pushed in the last 24h")

        for p in prs[:3]:
            lines.append(f"• `{p.repo}`: PR #{p.number} still open ({p.title[:60]})")

        if agent_branches:
            lines.append("• Unmerged agent branches: " + ", ".join(f"`{b}`" for b in agent_branches[:4]))

        if handoff:
            lines.append(f"\n**Left off:** \"{handoff['body'][:200]}\"")

        lines.append("\n**What's the focus today? Any blockers?**\n↳ *Reply in the thread below*")
        embed = header("checkin", subtitle="morning standup", description="\n".join(lines)[:4000], when=now)
        msg = await channel.send(embed=embed)
        thread = await msg.create_thread(name=f"Check-in {now:%b %d}", auto_archive_duration=1440)
        await self.bot.db.add_thread(thread.id, "checkin", f"Daily check-in {now:%Y-%m-%d}")

    async def post_evening_wrapup(self, channel: discord.TextChannel) -> None:
        now = datetime.now(self._tz)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = await self.bot.db.get_journal_entries_since(start_of_day)
        commits, prs, _ = await self.bot.github.recent_activity(hours=14)

        log_text = "\n".join(f"- [{e['kind']}] {e['body']}" for e in entries) or "(no manual logs today)"
        commit_text = "\n".join(f"- {c.repo} ({c.sha}): {c.message}" for c in commits) or "(no commits pushed today)"

        prompt = f"Today's journal entries:\n{log_text}\n\nToday's GitHub commits:\n{commit_text}"
        summary = await self.bot.llm.chat(
            FAST,
            [{"role": "system", "content": WRAPUP_SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=600,
        )

        notion_url = ""
        if self.bot.notion.enabled:
            try:
                md_section = f"## End-of-Day Wrap-Up ({now:%H:%M})\n\n{summary}\n\n### Commits\n{commit_text}"
                _, notion_url = await self.bot.notion.append_to_journal(now.date(), md_section)
            except Exception:
                log.exception("Failed appending evening wrap-up to Notion")

        desc = summary
        if notion_url:
            desc += f"\n\n🔗 **Notion Journal:** {notion_url}"
        desc += "\n\n**Where are you leaving off for tomorrow?**\n↳ *Reply in the thread below*"

        embed = header("wrapup", subtitle="daily summary", description=desc[:4000], when=now)
        msg = await channel.send(embed=embed)
        thread = await msg.create_thread(name=f"Wrap-up {now:%b %d}", auto_archive_duration=1440)
        await self.bot.db.add_thread(thread.id, "wrapup", f"Wrap-up {now:%Y-%m-%d}")

    async def post_weekly_retro(self, channel: discord.TextChannel) -> None:
        now = datetime.now(self._tz)
        week_ago = now - timedelta(days=7)
        entries = await self.bot.db.get_journal_entries_since(week_ago)
        commits, prs, agent_branches = await self.bot.github.recent_activity(hours=24 * 7)

        log_text = "\n".join(
            f"- {e['created_at'].astimezone(self._tz):%a %b %d} [{e['kind']}]: {e['body']}" for e in entries
        ) or "(no journal entries this week)"
        commit_text = "\n".join(f"- {c.repo} ({c.sha}): {c.message}" for c in commits[:30]) or "(no commits this week)"
        stale_text = ", ".join(agent_branches) if agent_branches else "none"

        prompt = (
            f"Week ending {now:%Y-%m-%d}\n\n"
            f"Journal Entries:\n{log_text[:10000]}\n\n"
            f"GitHub Commits:\n{commit_text[:6000]}\n\n"
            f"Open PRs: {len(prs)}\n"
            f"Unmerged agent/* branches: {stale_text}"
        )
        retro_md = await self.bot.llm.chat(
            SMART,
            [{"role": "system", "content": RETRO_SYSTEM}, {"role": "user", "content": prompt}],
            max_tokens=1200,
        )

        notion_url = ""
        if self.bot.notion.enabled:
            try:
                _, notion_url = await self.bot.notion.create_note(
                    title=f"Weekly Retro {now:%Y-%m-%d}",
                    note_type="Retro",
                    markdown_body=retro_md,
                    when=now.date(),
                )
            except Exception:
                log.exception("Failed saving weekly retro to Notion")

        desc = retro_md
        if notion_url:
            desc += f"\n\n🔗 **Notion Retro:** {notion_url}"
        embed = header("retro", subtitle=f"Week of {week_ago:%b %d} – {now:%b %d}", description=desc[:4000], when=now)
        await channel.send(embed=embed)

    async def run_research_spike(self, query: str) -> tuple[str, str]:
        """Search SearXNG, synthesize candidate fixes, and save to Notion."""
        hits = await self.bot.searxng.search(query, limit=6)
        snippets = "\n\n".join(
            f"[{i + 1}] {h.title}\nURL: {h.url}\nSnippet: {h.snippet}" for i, h in enumerate(hits)
        ) or "(SearXNG returned no results)"

        user_prompt = f"Developer blocker / research query: {query}\n\nWeb search results:\n{snippets}"
        report = await self.bot.llm.chat(
            SMART,
            [{"role": "system", "content": RESEARCH_SPIKE_SYSTEM}, {"role": "user", "content": user_prompt}],
            max_tokens=1200,
        )

        notion_url = ""
        if self.bot.notion.enabled:
            try:
                _, notion_url = await self.bot.notion.create_note(
                    title=f"Spike: {query[:90]}",
                    note_type="Research",
                    markdown_body=report,
                    when=datetime.now(self._tz).date(),
                )
            except Exception:
                log.exception("Failed saving research spike to Notion")
        return report, notion_url

    # --- Thread replies --------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.bot.is_owner_message(message) or not isinstance(message.channel, discord.Thread):
            return
        record = await self.bot.db.get_thread(message.channel.id)
        if record is None or record["kind"] not in ("checkin", "wrapup"):
            return

        text = message.content.strip()
        if not text:
            return
        now = datetime.now(self._tz)

        if record["kind"] == "checkin":
            await self.bot.db.add_journal_entry("plan", text)
            await asyncio.to_thread(append_journal_line, self.bot.cfg.data_dir, now, f"[plan] {text}")
            notion_url = ""
            if self.bot.notion.enabled:
                try:
                    _, notion_url = await self.bot.notion.append_to_journal(
                        now.date(),
                        f"## Morning Check-In ({now:%H:%M})\n\n{text}",
                    )
                except Exception:
                    log.exception("Failed syncing check-in plan to Notion")

            ack = "✅ Logged today's plan." + (f" ([Notion]({notion_url}))" if notion_url else "")
            await message.channel.send(ack)

            if BLOCKER_RE.search(text):
                await self.bot.db.add_journal_entry("blocker", text)
                await message.channel.send("🧭 Detected a blocker—running a SearXNG research spike…")
                report, spike_url = await self.run_research_spike(text)
                prefix = f"🧭 **Research Spike**" + (f" · [Notion]({spike_url})" if spike_url else "") + "\n\n"
                for chunk in split_message(prefix + report):
                    await message.channel.send(chunk)

        elif record["kind"] == "wrapup":
            await self.bot.db.add_journal_entry("handoff", text)
            await asyncio.to_thread(append_journal_line, self.bot.cfg.data_dir, now, f"[handoff] {text}")
            notion_url = ""
            if self.bot.notion.enabled:
                try:
                    _, notion_url = await self.bot.notion.append_to_journal(
                        now.date(),
                        f"### Handoff for Tomorrow ({now:%H:%M})\n\n{text}",
                    )
                except Exception:
                    log.exception("Failed syncing handoff to Notion")
            ack = "📌 Saved handoff for tomorrow's 8:00 AM check-in." + (f" ([Notion]({notion_url}))" if notion_url else "")
            await message.channel.send(ack)
