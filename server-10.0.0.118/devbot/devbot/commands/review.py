"""/review: fetch any GitHub PR diff and post a second-opinion review in Discord."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import header
from ..github import parse_pr_reference
from ..llm import SMART
from ..prompts import REVIEW_SYSTEM
from ..textutil import split_message
from .ask import PLACEHOLDER

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)


class ReviewApplyView(discord.ui.View):
    def __init__(
        self,
        bot: DevBot,
        repo_full: str,
        head_branch: str,
        pr_num: int,
        pr_url: str,
        review_md: str,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.repo_full = repo_full
        self.head_branch = head_branch
        self.pr_num = pr_num
        self.pr_url = pr_url
        self.review_md = review_md

    @discord.ui.button(label="Retry PR with Review Advice", style=discord.ButtonStyle.primary, emoji="🛠️")
    async def apply_review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can apply review fixes.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(view=self)

        channel = interaction.channel
        if not isinstance(channel, (discord.Thread, discord.TextChannel)):
            return

        code_cog = self.bot.get_cog("CodeCog")
        code_record = await self.bot.db.get_code_task(channel.id) if isinstance(channel, discord.Thread) else None

        if code_cog is not None and code_record is not None and isinstance(channel, discord.Thread):
            code_cog._spawn_task(channel.id, code_cog.run_followup_fix(channel, self.review_md))  # type: ignore[attr-defined]
            return

        async def _run_standalone() -> None:
            progress = await channel.send(
                f"🔧 **Applying PR #{self.pr_num} review advice on `{self.repo_full}` → `{self.head_branch}`…**"
            )

            async def update_progress(log_text: str) -> None:
                await progress.edit(content=log_text[:2000])

            outcome = await self.bot.runner.followup_fix(
                thread_id=channel.id,
                repo_full=self.repo_full,
                branch=self.head_branch,
                task=f"Apply PR #{self.pr_num} review feedback",
                plan="",
                feedback=self.review_md,
                on_progress=update_progress,
            )
            if not outcome.ok:
                await progress.edit(content=f"⚠️ **Review fix failed on `{self.head_branch}`**\n```\n{outcome.error[:1700]}\n```")
                return
            summary = (
                f"✅ **Pushed Review Fixes to `{self.head_branch}` (`{outcome.commit_sha}`)**\n"
                f"🔗 **PR:** {self.pr_url}\n\n"
                f"**Diff Stat:**\n```\n{outcome.diff_stat[:900]}\n```\n"
                f"**Verification:**\n```\n{outcome.test_output[:500]}\n```"
            )
            await channel.send(summary[:2000])

        asyncio.create_task(_run_standalone())

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can dismiss this.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="👌 Review acknowledged (no changes applied).", view=self)


class ReviewCog(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="review", description="Get a second-opinion code review on any GitHub PR diff")
    @app_commands.describe(pr="PR URL ('https://github.com/owner/repo/pull/12') or 'repo#12'")
    async def review(self, interaction: discord.Interaction, pr: str) -> None:
        if not self.bot.github.enabled:
            await interaction.response.send_message("⚠️ `GITHUB_TOKEN` is not configured.", ephemeral=True)
            return

        await interaction.response.defer()
        owner = await self.bot.github.username()
        parsed = parse_pr_reference(pr, default_owner=owner)
        if not parsed:
            await interaction.followup.send(
                "Could not parse PR reference. Pass a URL like `https://github.com/owner/repo/pull/12` or `repo#12`.",
                ephemeral=True,
            )
            return

        repo_full, pr_num = parsed
        try:
            meta, diff_text = await self.bot.github.get_pr_diff(repo_full, pr_num)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Failed fetching PR `{repo_full}#{pr_num}`: `{exc}`", ephemeral=True)
            return

        title = meta.get("title", f"PR #{pr_num}")
        html_url = meta.get("html_url", "")
        body = (meta.get("body") or "").strip()

        desc = f"**[{repo_full}#{pr_num}: {title}]({html_url})**"
        embed = header("review", subtitle=f"{repo_full}#{pr_num}", description=desc)
        starter = await interaction.followup.send(embed=embed, wait=True)

        if isinstance(interaction.channel, discord.TextChannel):
            target = await interaction.channel.create_thread(
                name=f"Review: {repo_full}#{pr_num}"[:95],
                message=starter,
                auto_archive_duration=1440,
            )
        else:
            target = interaction.channel  # type: ignore[assignment]

        placeholder = await target.send(PLACEHOLDER)
        prompt = (
            f"PR: {repo_full}#{pr_num} — {title}\n"
            + (f"Description:\n{body[:2000]}\n\n" if body else "\n")
            + f"Unified Diff (truncated to 20k chars if larger):\n```diff\n{diff_text[:20000]}\n```"
        )
        try:
            review_md = await self.bot.llm.chat(
                SMART,
                [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": prompt}],
                max_tokens=1500,
            )
        except Exception as exc:
            await placeholder.edit(content=f"⚠️ Review model call failed: `{exc}`"[:2000])
            return

        chunks = split_message(review_md) or ["(empty review)"]
        await placeholder.edit(content=chunks[0])
        for c in chunks[1:]:
            await target.send(c)

        head_branch = (meta.get("head") or {}).get("ref", "")
        if head_branch.startswith("agent/"):
            view = ReviewApplyView(self.bot, repo_full, head_branch, pr_num, html_url, review_md)
            await target.send(
                f"Want `devbot` to apply this review advice and push a new commit to `{head_branch}`?",
                view=view,
            )
