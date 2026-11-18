"""Consistent headers so every devbot post in #general is recognisable at a glance."""

from datetime import datetime

import discord

# kind -> (emoji + label, colour)
STYLES: dict[str, tuple[str, int]] = {
    "ask": ("💬 ASK", 0x3498DB),
    "note": ("📓 TECH NOTE", 0x2980B9),
    "doc": ("📄 DOC Q&A", 0x16A085),
    "code": ("🛠️ CODE TASK", 0xE67E22),
    "review": ("🔍 PR REVIEW", 0x8E44AD),
    "research": ("🧭 RESEARCH SPIKE", 0xD35400),
    "resume": ("📌 WHERE YOU LEFT OFF", 0x27AE60),
    "log": ("📝 LOGGED", 0x95A5A6),
    "status": ("🩺 DEVBOT STATUS", 0x1ABC9C),
    "checkin": ("☀️ DAILY CHECK-IN", 0xF1C40F),
    "wrapup": ("🌙 END-OF-DAY WRAP-UP", 0x9B59B6),
    "retro": ("📊 WEEKLY RETRO", 0x2ECC71),
}


def header(kind: str, subtitle: str = "", description: str | None = None, when: datetime | None = None) -> discord.Embed:
    label, colour = STYLES.get(kind, ("🤖 DEVBOT", 0x3498DB))
    title = label
    if when is not None:
        title += f" · {when:%a, %b} {when.day}"
    if subtitle:
        title += f" · {subtitle}"
    embed = discord.Embed(title=title[:256], description=description, colour=colour)
    embed.set_footer(text="devbot")
    return embed
