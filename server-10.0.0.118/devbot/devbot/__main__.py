import asyncio
import logging

import discord

from .bot import DevBot
from .commands.status import RingLogHandler
from .config import Config
from .db import Database
from .github import GitHubClient
from .llm import LLM
from .notion import NotionClient
from .searxng import SearXNGClient

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ring = RingLogHandler()
    ring.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(ring)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = Config.from_env()
    db = await Database.connect(cfg.database_url)
    llm = LLM(cfg.llm_base_url, cfg.llm_api_key)
    notion = NotionClient(cfg.notion_token, cfg.notion_root_page_id)
    github = GitHubClient(cfg.github_token)
    searxng = SearXNGClient(cfg.searxng_url)

    bot = DevBot(cfg, db, llm, notion, github, searxng, message_content_intent=True)
    try:
        try:
            await bot.start(cfg.discord_token)
        except discord.PrivilegedIntentsRequired:
            await bot.close()
            log.warning(
                "Message Content Intent is not enabled in Discord Developer Portal yet; "
                "reconnecting without it (slash commands and @mentions still work!)."
            )
            bot = DevBot(cfg, db, llm, notion, github, searxng, message_content_intent=False)
            await bot.start(cfg.discord_token)
    finally:
        await bot.close()
        await notion.close()
        await github.close()
        await searxng.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
