"""/code: plan -> approve in Discord -> run Aider on agent/* branch -> push & open Draft PR."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..docs_rag import KNOWN_DOC_SOURCES, doc_collection_key
from ..embeds import header
from ..change_contract import ChangeContract
from ..commit_titles import propose_commit_title, validate_commit_title
from ..github import slugify_branch
from ..llm import SMART
from ..prompts import CODE_PLAN_SYSTEM
from ..runner import detect_stack_versions
from ..task_memory import task_specification
from ..textutil import split_message
from .ask import _short, TEXT_EXTENSIONS

if TYPE_CHECKING:
    from ..bot import DevBot

log = logging.getLogger(__name__)


def _explicit_paths(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]


def _verification_data(outcome) -> dict:
    return outcome.verification or {
        "passed": outcome.ok,
        "checks": [],
        "output": outcome.test_output,
    }


def _verification_summary(outcome) -> str:
    checks = _verification_data(outcome).get("checks") or []
    if not checks:
        return outcome.test_output[:500]
    return "\n".join(
        f"{check['component']}: {check['status']}"
        + (f" (exit {check['exit_code']}, {check['duration_seconds']:.3f}s)"
           if check.get("exit_code") is not None else "")
        for check in checks
    )[:700]


def _contract_summary(outcome) -> str:
    contract = outcome.change_contract
    if not contract:
        return "Not available (legacy push retry)."
    required = ", ".join(contract.get("required_paths") or ()) or "none"
    changed = ", ".join(contract.get("changed_paths") or ()) or "none"
    return f"{'PASS' if contract.get('ok') else 'FAIL'}; required: {required}; changed: {changed}"[:700]


class GroundTruthView(discord.ui.View):
    def __init__(self, cog: CodeCog, thread_id: int, detected: list[tuple[str, str, str]]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id
        self.detected = detected

    @discord.ui.button(label="Accept Ground Truth", style=discord.ButtonStyle.primary, emoji="📚")
    async def accept_docs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.cog.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can configure ground truth.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.defer()
        doc_keys = await self.cog.bot.docs_rag.ensure_stack_docs(self.detected)
        await self.cog._set_ground_truth_meta(self.thread_id, enabled=True, doc_keys=doc_keys)
        summary = "✅ **Ground truth accepted** (`go doc` + " + (
            ", ".join(f"`{k}`" for k in doc_keys) if doc_keys else "pinned dependency types"
        ) + ")"
        await interaction.edit_original_response(content=summary, view=self)

    @discord.ui.button(label="Reject Docs", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def reject_docs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.cog.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can configure ground truth.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await self.cog._set_ground_truth_meta(self.thread_id, enabled=False, doc_keys=[])
        await interaction.response.edit_message(
            content="🚫 **Ground-truth docs skipped** (using repository source files only).",
            view=self,
        )


class CodeApprovalView(discord.ui.View):
    def __init__(self, cog: CodeCog, thread_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id

    @discord.ui.button(label="Approve & Run Aider", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.cog.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can approve code tasks.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(view=self)
        if isinstance(interaction.channel, discord.Thread):
            self.cog._spawn_task(interaction.channel.id, self.cog.run_approved_task(interaction.channel))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.cog.bot.cfg.owner_id:
            await interaction.response.send_message("Only the bot owner can cancel code tasks.", ephemeral=True)
            return
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.defer()
        if isinstance(interaction.channel, discord.Thread):
            await self.cog.cancel_thread_task(interaction.channel, announce=False)
        await interaction.edit_original_response(content="🚫 Code task cancelled.", view=self)


class CodeCog(commands.Cog):
    def __init__(self, bot: DevBot) -> None:
        self.bot = bot
        self.active_tasks: dict[int, asyncio.Task[None]] = {}

    @app_commands.command(name="code", description="Plan a small/medium code task, then run it on an agent/* branch")
    @app_commands.describe(
        repo="GitHub repository ('RepoName' or 'owner/RepoName')",
        task="What to build or change (keep it focused and self-contained)",
        model="Model alias from litellm/config.yaml (default: smart)",
    )
    @app_commands.choices(
        model=[
            app_commands.Choice(name="smart (qwen3-coder:30b)", value="smart"),
            app_commands.Choice(name="fast (qwen3:4b - fits in RX 550)", value="fast"),
        ]
    )
    async def code(self, interaction: discord.Interaction, repo: str, task: str, model: str = "smart") -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /code in a text channel.", ephemeral=True)
            return
        if not self.bot.github.enabled:
            await interaction.response.send_message("⚠️ `GITHUB_TOKEN` is not configured.", ephemeral=True)
            return

        await interaction.response.defer()
        full_repo = (await self.bot.github.find_repo_in_text("", explicit_project=repo)) or (
            await self.bot.github.resolve_repo(repo)
        )
        branch = f"{slugify_branch(task)}-{secrets.token_hex(4)}"

        desc = f"**Repo:** `{full_repo}` · **Branch:** `{branch}` · **Model:** `{model}`\n**Task:** {task[:3500]}"
        embed = header("code", subtitle=f"{full_repo} → {branch}", description=desc)
        starter = await interaction.followup.send(embed=embed, wait=True)
        thread = await interaction.channel.create_thread(
            name=f"Code: {_short(task, 80)}",
            message=starter,
            auto_archive_duration=1440,
        )
        await self.bot.db.add_thread(thread.id, "code", task, meta={"repo": full_repo, "branch": branch, "model": model})

        status_msg = await thread.send("📥 Cloning repository and drafting implementation plan…")
        detected: list[tuple[str, str, str]] = []
        try:
            repo_dir, outline = await self.bot.runner.prepare_workspace(thread.id, full_repo, task_hint=task)
            detected = await asyncio.to_thread(detect_stack_versions, repo_dir)
            conventions = await self.bot.notion.get_project_conventions(full_repo)
            user_prompt = (
                f"Repository: {full_repo}\n"
                f"Target Branch: {branch}\n"
                f"Task: {task}\n\n"
                + (f"Project Conventions (from Notion):\n{conventions}\n\n" if conventions else "")
                + f"{outline}"
            )
            plan = await self.bot.llm.chat(
                SMART,
                [{"role": "system", "content": CODE_PLAN_SYSTEM}, {"role": "user", "content": user_prompt}],
                max_tokens=1000,
            )
        except Exception as exc:
            log.exception("Failed preparing /code plan")
            await status_msg.edit(content=f"⚠️ Failed preparing code task: `{type(exc).__name__}: {exc}`"[:2000])
            return

        if detected:
            existing_rows = await self.bot.db.list_docs()
            existing_docs = {str(r["doc_name"]) for r in existing_rows}
            ver_lines = [f"• `{eco}` `{pkg}` **{ver}**" for eco, pkg, ver in detected[:12]]
            doc_sources: list[str] = []
            if any(eco == "go" for eco, _, _ in detected):
                doc_sources.append("• Live `go doc` signatures for pinned `go.mod` packages")
            for _eco, pkg, ver in detected:
                base_pkg = pkg.lower().lstrip("@").split("/")[0]
                if pkg.lower() in KNOWN_DOC_SOURCES or base_pkg in KNOWN_DOC_SOURCES:
                    key = doc_collection_key(base_pkg)
                    state = "cached in `pgvector`" if key in existing_docs else "auto-fetch official docs"
                    source = KNOWN_DOC_SOURCES.get(pkg.lower()) or KNOWN_DOC_SOURCES[base_pkg]
                    doc_sources.append(
                        f"• `{key}` ({state}; {source.version_label}; dependency declares `{ver}`)"
                    )
            gt_text = (
                "📚 **Detected Build & Ground-Truth Documentation**\n"
                + "\n".join(ver_lines)
                + "\n\n**Available Ground Truth:**\n"
                + ("\n".join(doc_sources) if doc_sources else "• Pinned repo dependencies only")
            )
            await thread.send(gt_text[:1950], view=GroundTruthView(self, thread.id, detected))

        commit_title = propose_commit_title(task)
        await self.bot.db.save_code_task(thread.id, full_repo, branch, task, plan, status="planned")
        await self.bot.db.create_code_subtask(
            thread.id, task, plan, model=model, commit_title=commit_title, status="planned",
        )
        chunks = split_message(
            f"📋 **Proposed Implementation Plan**\n\n{plan}\n\n**Commit:** `{commit_title}`"
        )
        await status_msg.edit(content=chunks[0])
        for c in chunks[1:]:
            await thread.send(c)

        view = CodeApprovalView(self, thread.id)
        await thread.send(
            "Approve to run Aider on an isolated `agent/*` branch, or reply `approve` in this thread:",
            view=view,
        )

    @app_commands.command(name="continue", description="Plan another commit on this thread's existing PR")
    @app_commands.describe(
        request="The next independently planned change",
        required_paths="Comma-separated repository-relative paths that must change",
        only_paths="Reject every changed path not listed in required_paths",
        commit_title="Optional Conventional Commit title",
    )
    async def continue_pr(
        self,
        interaction: discord.Interaction,
        request: str,
        required_paths: str,
        only_paths: bool = True,
        commit_title: str = "",
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Run `/continue` inside a `/code` thread.", ephemeral=True)
            return
        paths = _explicit_paths(required_paths)
        if not paths:
            await interaction.response.send_message(
                "`required_paths` must contain at least one repository-relative path.", ephemeral=True,
            )
            return
        await interaction.response.send_message("🧭 Continuation planning started in this thread.")
        self._spawn_task(
            interaction.channel.id,
            self.plan_continuation(interaction.channel, request, paths, only_paths, commit_title),
        )

    async def plan_continuation(
        self,
        thread: discord.Thread,
        request: str,
        required_paths: list[str],
        only_paths: bool,
        commit_title: str = "",
    ) -> None:
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_code_task(thread.id)
            if not record or record["status"] not in ("done", "failed"):
                await thread.send("`/continue` requires a completed or failed code task in this thread.")
                return
            status = await thread.send("🧭 Inspecting the current PR branch and planning the next subtask…")
            try:
                repo_dir, outline = await self.bot.runner.prepare_existing_workspace(
                    thread.id, record["repo"], record["branch"], request,
                )
                allowed_paths = required_paths if only_paths else None
                contract = ChangeContract.create(repo_dir, required_paths, allowed_paths)
                title = (
                    validate_commit_title(commit_title)
                    if commit_title.strip()
                    else propose_commit_title(request, contract.required_paths)
                )
                conventions = await self.bot.notion.get_project_conventions(record["repo"])
                latest = await self.bot.db.get_latest_code_subtask(thread.id)
                historical = latest["request"][:1000] if latest else record["task"][:1000]
                prompt = (
                    f"Repository: {record['repo']}\nCurrent PR branch: {record['branch']}\n"
                    f"NEW SUBTASK (this is the only active request):\n{request}\n\n"
                    f"Required paths: {list(contract.required_paths)}\n"
                    f"Allowed paths: {None if contract.allowed_paths is None else list(contract.allowed_paths)}\n\n"
                    f"Historical PR context only (not an approved plan):\n{historical}\n\n"
                    f"Project conventions:\n{conventions}\n\nCurrent branch evidence:\n{outline}"
                )
                plan = await self.bot.llm.chat(
                    SMART,
                    [{"role": "system", "content": CODE_PLAN_SYSTEM}, {"role": "user", "content": prompt}],
                    max_tokens=1000,
                )
                await self.bot.db.create_code_subtask(
                    thread.id, request, plan,
                    required_paths=contract.required_paths,
                    allowed_paths=contract.allowed_paths,
                    model=latest["model"] if latest else "smart",
                    commit_title=title,
                    status="planned",
                )
                await self.bot.db.update_code_task_status(thread.id, "planned")
            except Exception as exc:
                log.exception("Failed planning continued PR subtask")
                await status.edit(content=f"⚠️ Failed planning continuation: `{type(exc).__name__}: {exc}`"[:2000])
                return

            scope = (
                "unrestricted"
                if contract.allowed_paths is None
                else ", ".join(f"`{path}`" for path in contract.allowed_paths)
            )
            chunks = split_message(
                f"📋 **Continued PR Subtask Plan**\n\n{plan}\n\n"
                f"**Required:** {', '.join(f'`{path}`' for path in contract.required_paths) or 'none'}\n"
                f"**Allowed scope:** {scope}\n**Commit:** `{title}`"
            )
            await status.edit(content=chunks[0])
            for chunk in chunks[1:]:
                await thread.send(chunk)
            await thread.send("Approve this subtask to run it on the existing PR branch:", view=CodeApprovalView(self, thread.id))

    @app_commands.command(name="fix", description="Repair the current PR subtask from diagnostics or review feedback")
    @app_commands.describe(
        feedback="The concrete failure or review finding to repair",
        commit_title="Optional Conventional Commit title",
    )
    async def fix_pr(
        self, interaction: discord.Interaction, feedback: str, commit_title: str = "",
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Run `/fix` inside a `/code` thread.", ephemeral=True)
            return
        if commit_title.strip():
            try:
                validate_commit_title(commit_title)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message("🔧 Repair queued on the existing PR branch.")
        self._spawn_task(
            interaction.channel.id,
            self.run_followup_fix(interaction.channel, feedback, commit_title=commit_title),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.bot.is_owner_message(message) or not isinstance(message.channel, discord.Thread):
            return
        record = await self.bot.db.get_code_task(message.channel.id)
        if not record:
            return
        parts = [message.content.strip()]
        for attachment in message.attachments:
            if (attachment.content_type or "").startswith("text/") or any(
                attachment.filename.lower().endswith(ext) for ext in TEXT_EXTENSIONS
            ):
                if attachment.size > 1_000_000:
                    await message.channel.send("Attachment exceeds 1 MB; split it before requesting a code change.")
                    return
                content = (await attachment.read()).decode("utf-8", errors="replace")
                parts.append(f"Attachment {attachment.filename}:\n{content}")
        text = "\n\n".join(p for p in parts if p)
        if not text:
            return
        lower = text.lower()
        if lower in ("cancel", "abort", "stop"):
            await self.cancel_thread_task(message.channel)
            return
        if lower in ("approve", "approved", "yes", "run", "retry", "lgtm", "✅") and record["status"] in ("planned", "failed"):
            self._spawn_task(message.channel.id, self.run_approved_task(message.channel))
        elif lower in ("push", "retry push"):
            await self.push_only(message.channel)
        elif record["status"] in ("done", "failed") and (lower == "fix" or lower.startswith("fix ")):
            feedback = text[3:].strip()
            if not feedback:
                await message.channel.send("Include the failure or review feedback after `fix`, or use `/continue` for new work.")
                return
            self._spawn_task(message.channel.id, self.run_followup_fix(message.channel, feedback))
        else:
            active = self.active_tasks.get(message.channel.id)
            if record["status"] == "planned":
                await self.revise_plan(message.channel, text)
            elif record["status"] == "running" or (active and not active.done()):
                await message.channel.send(
                    "The current run uses an immutable instruction snapshot. Use `/fix` with diagnostics after it finishes."
                )
            else:
                await message.channel.send(
                    "This message did not change code or task memory. Use `/continue` for new work or `fix <details>` for a repair."
                )

    async def _set_ground_truth_meta(self, thread_id: int, enabled: bool, doc_keys: list[str]) -> None:
        t_rec = await self.bot.db.get_thread(thread_id)
        if not t_rec:
            return
        meta = json.loads(t_rec["meta"] or "{}") if isinstance(t_rec["meta"], str) else (t_rec["meta"] or {})
        meta["use_ground_truth"] = enabled
        meta["doc_keys"] = doc_keys
        await self.bot.db.add_thread(thread_id, t_rec["kind"], t_rec["seed"], meta=meta)

    async def _load_ground_truth_text(self, thread_id: int, query: str) -> str:
        t_rec = await self.bot.db.get_thread(thread_id)
        if not t_rec:
            return ""
        meta = json.loads(t_rec["meta"] or "{}") if isinstance(t_rec["meta"], str) else (t_rec["meta"] or {})
        if meta.get("use_ground_truth") is False:
            return ""
        doc_keys = meta.get("doc_keys") or []
        if not doc_keys:
            return ""
        return await self.bot.docs_rag.retrieve_for_stack(doc_keys, query)

    def _spawn_task(self, thread_id: int, coro) -> None:
        existing = self.active_tasks.get(thread_id)
        if existing and not existing.done():
            coro.close()
            return
        task = asyncio.create_task(coro)
        self.active_tasks[thread_id] = task

        def completed(done):
            if self.active_tasks.get(thread_id) is done:
                self.active_tasks.pop(thread_id, None)
            if not done.cancelled() and done.exception():
                log.error("Code task failed", exc_info=done.exception())

        task.add_done_callback(completed)

    @app_commands.command(name="cancel", description="Cancel the current /code plan or abort a running code task")
    async def cancel_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Run `/cancel` inside a `/code` thread.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        msg = await self.cancel_thread_task(interaction.channel, announce=True)
        await interaction.followup.send(msg, ephemeral=True)

    async def cancel_thread_task(self, thread: discord.Thread, announce: bool = True) -> str:
        running = self.active_tasks.pop(thread.id, None)
        if running and not running.done():
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
        await self.bot.db.update_code_task_status(thread.id, "cancelled")
        text = "🚫 Code task cancelled."
        if announce:
            await thread.send(text)
        return text

    async def revise_plan(self, thread: discord.Thread, feedback: str) -> None:
        """Update an unapproved /code plan based on developer feedback in the thread."""
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_code_task(thread.id)
            if not record or record["status"] != "planned":
                return
            status_msg = await thread.send("📝 Revising implementation plan based on your feedback…")
            subtask = await self.bot.db.get_latest_code_subtask(thread.id)
            specification = subtask["request"] if subtask else record["task"]
            repo_dir = self.bot.runner.scratch_root / str(thread.id) / "repo"
            if not repo_dir.exists():
                repo_dir, _ = await self.bot.runner.prepare_workspace(thread.id, record["repo"], specification)
            outline = await self.bot.runner.build_planning_context(repo_dir, specification)
            conventions = await self.bot.notion.get_project_conventions(record["repo"])
            user_prompt = (
                f"Repository: {record['repo']}\n"
                f"Task specification:\n{specification}\n\nRepository evidence:\n{outline}\n\nConventions:\n{conventions}\n\n"
                f"Previous Plan:\n{subtask['plan'] if subtask else record['plan']}\n\n"
                f"Developer Feedback / Changes Requested:\n{feedback}\n\n"
                "Provide the revised, concise implementation plan."
            )
            new_plan = await self.bot.llm.chat(
                SMART,
                [{"role": "system", "content": CODE_PLAN_SYSTEM}, {"role": "user", "content": user_prompt}],
                max_tokens=1000,
            )
            await self.bot.db.save_code_task(
                thread.id,
                record["repo"],
                record["branch"],
                record["task"],
                new_plan,
                status="planned",
            )
            if subtask:
                await self.bot.db.update_code_subtask_plan(subtask["id"], new_plan)
            chunks = split_message(f"📋 **Revised Implementation Plan**\n\n{new_plan}")
            await status_msg.edit(content=chunks[0])
            for c in chunks[1:]:
                await thread.send(c)
            view = CodeApprovalView(self, thread.id)
            await thread.send("Approve this revised plan, or reply with more changes:", view=view)

    async def run_followup_fix(
        self, thread: discord.Thread, feedback: str, commit_title: str = "",
    ) -> None:
        """Run a targeted repair pass for pasted CI/CD errors or review comments and push to the same branch."""
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_code_task(thread.id)
            if not record:
                return
            previous = await self.bot.db.get_latest_code_subtask(thread.id)
            repo_full = record["repo"]
            branch = record["branch"]
            plan = previous["plan"] if previous else record["plan"]
            required_paths = tuple(previous["required_paths"]) if previous else ()
            allowed_paths = (
                tuple(previous["allowed_paths"])
                if previous and previous["allowed_paths"] is not None else None
            )
            model_alias = previous["model"] if previous else "smart"
            fix_title = (
                validate_commit_title(commit_title)
                if commit_title.strip()
                else propose_commit_title(feedback, required_paths, kind="fix")
            )
            subtask = await self.bot.db.create_code_subtask(
                thread.id,
                f"Repair current PR subtask:\n{feedback}",
                plan,
                required_paths=required_paths,
                allowed_paths=allowed_paths,
                model=model_alias,
                commit_title=fix_title,
                status="running",
            )
            task = subtask["request"]

            progress = await thread.send(
                f"🔧 **Running Targeted Fix on `{repo_full}` → `{branch}`**\n⏳ Analyzing error / feedback…"
            )

            async def update_discord_progress(log_text: str) -> None:
                await progress.edit(content=log_text[:2000])

            conventions = await self.bot.notion.get_project_conventions(repo_full)
            ground_truth = await self._load_ground_truth_text(thread.id, f"{task}\n{plan}\n{feedback}")
            await self.bot.db.update_code_task_status(thread.id, "running")
            try:
                outcome = await self.bot.runner.followup_fix(
                    thread_id=thread.id,
                    repo_full=repo_full,
                    branch=branch,
                    task=task,
                    plan=plan,
                    feedback=feedback,
                    conventions=conventions,
                    ground_truth=ground_truth,
                    model=model_alias,
                    commit_title=fix_title,
                    required_paths=required_paths,
                    allowed_paths=allowed_paths,
                    on_progress=update_discord_progress,
                )
            except Exception as exc:
                await self.bot.db.update_code_task_status(thread.id, "failed")
                await self.bot.db.update_code_subtask_result(subtask["id"], "failed")
                await progress.edit(content=f"Targeted fix failed: {exc}"[:1900])
                return
            if not outcome.ok:
                await self.bot.db.update_code_task_status(thread.id, "failed")
                await self.bot.db.update_code_subtask_result(
                    subtask["id"], "failed",
                    base_sha=outcome.base_sha,
                    verification={**_verification_data(outcome), "error": outcome.error},
                )
                await progress.edit(content=f"⚠️ **Targeted fix failed on `{branch}`**\n```\n{outcome.error[:1700]}\n```")
                return

            pr_url = record["pr_url"] or await self._open_pr(repo_full, branch, task, plan, outcome.test_output)
            await self.bot.db.update_code_task_status(thread.id, "done", pr_url=pr_url)
            await self.bot.db.update_code_subtask_result(
                subtask["id"], "done",
                base_sha=outcome.base_sha,
                verification=_verification_data(outcome),
                commit_sha=outcome.commit_sha,
            )
            summary = (
                f"✅ **Pushed Fix to `{branch}` (`{outcome.commit_sha}`)**\n"
                f"📝 **Commit:** `{fix_title}`\n"
                f"🔗 **Review / Draft PR:** {pr_url}\n\n"
                f"**Latest Commit Diff:**\n```\n{outcome.diff_stat[:1000]}\n```\n"
                f"**Cumulative PR Diff:**\n```\n{outcome.cumulative_diff_stat[:700]}\n```\n"
                f"**Path Contract:**\n```\n{_contract_summary(outcome)}\n```\n"
                f"**Verification:**\n```\n{_verification_summary(outcome)}\n```"
            )
            await thread.send(summary[:2000])

    async def push_only(self, thread: discord.Thread) -> None:
        """Push an already-committed branch in the scratch workspace and open the Draft PR."""
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_code_task(thread.id)
            if not record:
                return
            subtask = await self.bot.db.get_latest_code_subtask(thread.id)
            repo_full = record["repo"]
            branch = record["branch"]
            task = record["task"]
            plan = record["plan"]
            msg = await thread.send(f"🚀 Pushing existing commit on `{branch}` to `{repo_full}`…")
            outcome = await self.bot.runner.push_existing_branch(thread.id, repo_full, branch)
            if not outcome.ok:
                await msg.edit(content=f"⚠️ **Push failed on `{branch}`**\n```\n{outcome.error[:1700]}\n```")
                return
            pr_url = await self._open_pr(repo_full, branch, task, plan, outcome.test_output)
            await self.bot.db.update_code_task_status(thread.id, "done", pr_url=pr_url)
            commit_line = f"📝 **Commit:** `{subtask['commit_title']}`\n" if subtask else ""
            summary = (
                f"✅ **Pushed `{branch}` (`{outcome.commit_sha}`)**\n"
                + commit_line
                + f"🔗 **Review / Draft PR:** {pr_url}\n\n"
                f"**Current Subtask / Latest Commit:**\n```\n{outcome.diff_stat[:800]}\n```\n"
                f"**Cumulative PR Diff:**\n```\n{outcome.cumulative_diff_stat[:700]}\n```\n"
                f"**Path Contract:**\n```\n{_contract_summary(outcome)}\n```\n"
                f"**Verification:**\n```\n{_verification_summary(outcome)}\n```\n"
                f"To review in VS Code:\n```bash\ngit fetch origin && git checkout {branch}\n```"
            )
            await msg.edit(content=summary[:2000])

    async def _open_pr(self, repo_full: str, branch: str, task: str, plan: str, test_output: str) -> str:
        try:
            default_branch = await self.bot.github.get_default_branch(repo_full)
            pr_body = (
                f"## Automated Code Task (`devbot`)\n\n"
                f"**Task:** {task}\n\n"
                f"### Implementation Plan\n{plan}\n\n"
                f"### Verification\n```\n{test_output}\n```"
            )
            return await self.bot.github.create_draft_pr(
                repo_full=repo_full,
                head_branch=branch,
                base_branch=default_branch,
                title=f"[agent] {task.splitlines()[0][:120]}",
                body=pr_body,
            )
        except Exception as exc:
            log.warning("Draft PR creation failed (branch was still pushed): %s", exc)
            return f"https://github.com/{repo_full}/tree/{branch}"

    async def run_approved_task(self, thread: discord.Thread) -> None:
        async with self.bot.lock_for(thread.id):
            record = await self.bot.db.get_code_task(thread.id)
            if not record or record["status"] not in ("planned", "failed"):
                return

            subtask = await self.bot.db.get_latest_code_subtask(thread.id)
            await self.bot.db.update_code_task_status(thread.id, "running")
            if subtask:
                await self.bot.db.update_code_subtask_result(subtask["id"], "running")
            repo_full = record["repo"]
            branch = record["branch"]
            task = subtask["request"] if subtask else task_specification(
                self.bot.cfg.data_dir, thread.id, record["task"]
            )
            plan = subtask["plan"] if subtask else record["plan"]

            progress = await thread.send(
                f"🛠️ **Running Code Task on `{repo_full}` → `{branch}`**\n⏳ Preparing target files…"
            )
            think_msg = await thread.send(
                "💭 *Waiting for the local model response; this Aider pass does not stream partial output…*"
            )

            async def update_discord_progress(log_text: str) -> None:
                await progress.edit(content=log_text[:2000])

            async def update_discord_think(think_text: str) -> None:
                await think_msg.edit(content=think_text[:2000])

            try:
                conventions = await self.bot.notion.get_project_conventions(repo_full)
                ground_truth = await self._load_ground_truth_text(thread.id, f"{task}\n{plan}")
                t_rec = await self.bot.db.get_thread(thread.id)
                t_meta = json.loads(t_rec["meta"] or "{}") if (t_rec and isinstance(t_rec["meta"], str)) else ((t_rec["meta"] if t_rec else None) or {})
                model_alias = subtask["model"] if subtask else t_meta.get("model", "smart")
                outcome = await self.bot.runner.execute_task(
                    thread_id=thread.id,
                    repo_full=repo_full,
                    branch=branch,
                    task=task,
                    plan=plan,
                    conventions=conventions,
                    ground_truth=ground_truth,
                    model=model_alias,
                    commit_title=subtask["commit_title"] if subtask else "",
                    required_paths=tuple(subtask["required_paths"]) if subtask else (),
                    allowed_paths=(
                        tuple(subtask["allowed_paths"])
                        if subtask and subtask["allowed_paths"] is not None else None
                    ),
                    on_progress=update_discord_progress,
                    on_think=update_discord_think,
                )
            except Exception as exc:
                log.exception("Code runner crashed")
                await self.bot.db.update_code_task_status(thread.id, "failed")
                if subtask:
                    await self.bot.db.update_code_subtask_result(subtask["id"], "failed")
                await progress.edit(content=f"⚠️ Code runner failed: `{type(exc).__name__}: {exc}`"[:2000])
                return

            if not outcome.ok:
                await self.bot.db.update_code_task_status(thread.id, "failed")
                if subtask:
                    await self.bot.db.update_code_subtask_result(
                        subtask["id"], "failed", base_sha=outcome.base_sha,
                        verification={**_verification_data(outcome), "error": outcome.error},
                    )
                await progress.edit(content=f"⚠️ **Code run failed on `{branch}`**\n```\n{outcome.error[:1700]}\n```")
                return

            pr_url = await self._open_pr(repo_full, branch, task, plan, outcome.test_output)
            await self.bot.db.update_code_task_status(thread.id, "done", pr_url=pr_url)
            if subtask:
                await self.bot.db.update_code_subtask_result(
                    subtask["id"], "done", base_sha=outcome.base_sha,
                    verification=_verification_data(outcome),
                    commit_sha=outcome.commit_sha,
                )
            commit_line = f"📝 **Commit:** `{subtask['commit_title']}`\n" if subtask else ""
            summary = (
                f"✅ **Pushed `{branch}` (`{outcome.commit_sha}`)**\n"
                + commit_line
                + f"🔗 **Review / Draft PR:** {pr_url}\n\n"
                f"**Current Subtask / Latest Commit:**\n```\n{outcome.diff_stat[:800]}\n```\n"
                f"**Cumulative PR Diff:**\n```\n{outcome.cumulative_diff_stat[:700]}\n```\n"
                f"**Path Contract:**\n```\n{_contract_summary(outcome)}\n```\n"
                f"**Verification:**\n```\n{_verification_summary(outcome)}\n```\n"
                f"To review in VS Code:\n```bash\ngit fetch origin && git checkout {branch}\n```"
            )
            await progress.edit(content=summary[:2000])
