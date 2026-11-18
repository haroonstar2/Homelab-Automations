"""/doc: upload or paste documentation, embed into pgvector, and ask grounded questions in a thread."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..embeds import header
from ..llm import SMART
from ..prompts import DOC_QA_SYSTEM
from ..textutil import split_message
from .ask import ERROR_PREFIX, PLACEHOLDER, _short

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)


class DocCog(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot

    @app_commands.command(name="doc", description="Index documentation into pgvector or open an existing doc for Q&A")
    @app_commands.describe(
        name="Short identifier for this document (e.g., 'stripe-webhooks', 'fastapi-deps')",
        question="Optional first question to ask right away",
        file="Optional text/markdown file attachment to ingest",
        text="Optional pasted documentation snippet to ingest",
    )
    async def doc(
        self,
        interaction: discord.Interaction,
        name: str,
        question: str = "",
        file: discord.Attachment | None = None,
        text: str = "",
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /doc in a text channel.", ephemeral=True)
            return

        doc_name = name.strip().lower().replace(" ", "-")
        await interaction.response.defer()

        raw_doc = text.strip()
        if file is not None:
            data = await file.read()
            file_text = data.decode("utf-8", errors="replace").strip()
            raw_doc = f"{raw_doc}\n\n{file_text}".strip() if raw_doc else file_text
            # Save a backup copy on disk
            docs_dir = self.bot.cfg.data_dir / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            (docs_dir / f"{doc_name}-{file.filename}").write_bytes(data)

        chunk_count = 0
        if raw_doc:
            chunk_count = await self.bot.docs_rag.ingest(doc_name, raw_doc)
            status_line = f"Indexed **{chunk_count}** chunks into `pgvector` for `{doc_name}`."
        else:
            status_line = f"Opened Q&A thread for indexed doc `{doc_name}`. (You can also drop files/text in the thread to re-index)."

        desc = status_line + (f"\n\n**Question:** {question}" if question else "\n\nAsk any question in the thread below.")
        embed = header("doc", subtitle=doc_name, description=desc)
        starter = await interaction.followup.send(embed=embed, wait=True)
        thread = await interaction.channel.create_thread(
            name=f"Doc: {_short(doc_name, 85)}",
            message=starter,
            auto_archive_duration=1440,
        )
        await self.bot.db.add_thread(thread.id, "doc", question or f"Doc Q&A: {doc_name}", meta={"doc_name": doc_name})

        if question.strip():
            await self._answer_doc_question(thread, doc_name, question.strip())

    @app_commands.command(name="docs", description="List all documentation sets indexed in pgvector")
    async def docs(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_docs()
        if not rows:
            await interaction.response.send_message("No documents indexed yet. Use `/doc` to add one.", ephemeral=True)
            return
        lines = [f"- `{r['doc_name']}` — **{r['chunks']}** chunks (updated {r['updated_at']:%Y-%m-%d})" for r in rows]
        embed = header("doc", subtitle="Indexed Documents", description="\n".join(lines)[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.bot.is_owner_message(message) or not isinstance(message.channel, discord.Thread):
            return
        record = await self.bot.db.get_thread(message.channel.id)
        if record is None or record["kind"] != "doc":
            return
        meta = json.loads(record["meta"] or "{}") if isinstance(record["meta"], str) else (record["meta"] or {})
        doc_name = meta.get("doc_name", "")
        if not doc_name:
            return

        # If the user attaches a new file in the thread, ingest it into this doc_name first
        for att in message.attachments:
            raw = (await att.read()).decode("utf-8", errors="replace")
            if raw.strip():
                n_chunks = await self.bot.docs_rag.ingest(doc_name, raw)
                await message.channel.send(f"📥 Indexed `{att.filename}` into `{doc_name}` (**{n_chunks}** chunks).")

        if message.content.strip():
            await self._answer_doc_question(message.channel, doc_name, message.content.strip())

    async def _answer_doc_question(self, thread: discord.Thread, doc_name: str, question: str) -> None:
        async with self.bot.lock_for(thread.id):
            placeholder = await thread.send(PLACEHOLDER)
            try:
                async with thread.typing():
                    hits = await self.bot.docs_rag.retrieve(doc_name, question, top_k=5)
                    if not hits:
                        await placeholder.edit(
                            content=f"⚠️ No indexed chunks found for `{doc_name}`. Attach a file or paste text with `/doc` first."
                        )
                        return
                    excerpts = "\n\n".join(
                        f"[chunk {idx}] (similarity {score:.2f}):\n{content}"
                        for idx, score, content in hits
                    )
                    user_prompt = (
                        f"Document: {doc_name}\n\n"
                        f"Retrieved Excerpts:\n{excerpts}\n\n"
                        f"Question: {question}"
                    )
                    messages = [
                        {"role": "system", "content": DOC_QA_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ]
                    reply = await self.bot.llm.chat(SMART, messages)
            except Exception as exc:
                log.exception("doc QA failed")
                await placeholder.edit(content=f"{ERROR_PREFIX} Doc Q&A failed: `{type(exc).__name__}: {exc}`"[:2000])
                return

            chunks = split_message(reply) or ["(empty response)"]
            await placeholder.edit(content=chunks[0])
            for c in chunks[1:]:
                await thread.send(c)
