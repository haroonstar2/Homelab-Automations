import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .db import Database
from .docs_rag import DocsRAG
from .github import GitHubClient
from .llm import LLM
from .notion import NotionClient
from .runner import CodeRunner
from .searxng import SearXNGClient

log = logging.getLogger(__name__)


class OwnerOnlyTree(app_commands.CommandTree):
    """Every slash command is rejected unless it comes from DISCORD_OWNER_ID."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.client.cfg.owner_id:  # type: ignore[attr-defined]
            return True
        await interaction.response.send_message("devbot only responds to its owner.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("command failed", exc_info=error)
        msg = f"⚠️ Command failed: `{type(error).__name__}: {error}`"[:1900]
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class DevBot(commands.Bot):
    def __init__(
        self,
        cfg: Config,
        db: Database,
        llm: LLM,
        notion: NotionClient,
        github: GitHubClient,
        searxng: SearXNGClient,
        message_content_intent: bool = True,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = message_content_intent
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=OwnerOnlyTree,
            help_command=None,
        )
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.notion = notion
        self.github = github
        self.searxng = searxng
        self.docs_rag = DocsRAG(db, llm)
        self.runner = CodeRunner(
            cfg.data_dir, cfg.github_token, cfg.llm_base_url, cfg.llm_api_key,
            context_tokens=cfg.llm_context_tokens, output_tokens=cfg.llm_output_tokens,
            ollama_url=cfg.ollama_url, ollama_model=cfg.ollama_model,
        )
        self.thread_locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, thread_id: int) -> asyncio.Lock:
        return self.thread_locks.setdefault(thread_id, asyncio.Lock())

    def is_owner_message(self, message: discord.Message) -> bool:
        return not message.author.bot and message.author.id == self.cfg.owner_id

    async def setup_hook(self) -> None:
        await self.db.recover_interrupted_code_tasks()
        from .commands.ask import Ask
        from .commands.checkin import CheckinCog
        from .commands.code import CodeCog
        from .commands.doc import DocCog
        from .commands.journal import Journal
        from .commands.note import NoteCog
        from .commands.review import ReviewCog
        from .commands.status import Status

        if self.notion.enabled:
            try:
                await self.notion.ensure_databases()
                log.info("Notion databases verified (Notes + Projects)")
            except Exception:
                log.exception("Notion database bootstrap failed (will retry on first command)")

        await self.add_cog(Ask(self))
        await self.add_cog(Journal(self))
        await self.add_cog(Status(self))
        await self.add_cog(NoteCog(self))
        await self.add_cog(DocCog(self))
        await self.add_cog(CheckinCog(self))
        await self.add_cog(CodeCog(self))
        await self.add_cog(ReviewCog(self))

        # Guild-scoped sync updates instantly. Clear global commands so Discord doesn't show duplicates!
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d slash commands to guild %s", len(synced), self.cfg.guild_id)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info("cleared duplicate global slash commands")
        except discord.Forbidden:
            log.warning(
                "Missing Access (403) syncing to guild %s; Invite URL: "
                "https://discord.com/oauth2/authorize?client_id=%s&permissions=326417590272&scope=bot%%20applications.commands",
                self.cfg.guild_id,
                self.application_id,
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d slash commands on join to guild %s (%s)", len(synced), guild.name, guild.id)
        except Exception as exc:
            log.warning("could not sync on guild join %s: %s", guild.id, exc)

    async def on_ready(self) -> None:
        guild_list = [f"{g.name} ({g.id})" for g in self.guilds]
        log.info("logged in as %s (%s); guilds=%s", self.user, getattr(self.user, "id", "?"), guild_list)
        # If the bot is in a guild whose ID differs from DISCORD_GUILD_ID, sync to that guild too
        for g in self.guilds:
            if g.id != self.cfg.guild_id:
                try:
                    self.tree.copy_global_to(guild=g)
                    synced = await self.tree.sync(guild=g)
                    log.info("auto-synced %d slash commands to joined guild %s (%s)", len(synced), g.name, g.id)
                except Exception as exc:
                    log.warning("could not auto-sync to guild %s: %s", g.id, exc)
