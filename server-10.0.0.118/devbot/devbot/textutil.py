"""Pure helpers for Discord formatting and conversation context (unit-tested)."""

import re
from dataclasses import dataclass

DISCORD_LIMIT = 2000
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks some local models emit."""
    text = _THINK_RE.sub("", text)
    # Unterminated block (output cut off mid-thought): drop everything after it.
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks without breaking code fences.

    If a chunk boundary falls inside a ``` block, the block is closed at the end
    of the chunk and reopened (with the same language tag) in the next one.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    closing = "\n```"
    chunks: list[str] = []
    current = ""
    open_fence: str | None = None  # the opening line, e.g. "```python"

    def flush() -> None:
        nonlocal current
        body = current.rstrip("\n")
        if open_fence is not None:
            body += closing
        if body.strip():
            chunks.append(body)
        current = (open_fence + "\n") if open_fence is not None else ""

    for line in text.split("\n"):
        is_fence = line.strip().startswith("```")
        while True:
            reserve = len(closing) if open_fence is not None else 0
            room = limit - reserve - len(current) - 1  # -1 for the newline
            if len(line) <= room:
                current += line + "\n"
                break
            prefix_len = len(open_fence) + 1 if open_fence is not None else 0
            if len(current) > prefix_len:
                flush()  # chunk has content: start a new one and retry
                continue
            # Even an empty chunk can't hold this line: hard-split it.
            current += line[:room] + "\n"
            line = line[room:]
            flush()

        if is_fence:
            open_fence = None if open_fence is not None else line.strip()

    body = current.rstrip("\n")
    if body.strip() and body != open_fence:
        chunks.append(body)
    return chunks


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    content: str


def build_messages(
    system: str,
    seed: str,
    turns: list[Turn],
    budget_chars: int = 24_000,
) -> list[dict[str, str]]:
    """Build an OpenAI-style message list for a thread conversation.

    - The seed (original question) and the most recent turn are always kept.
    - Oldest turns in between are dropped first to stay under budget_chars,
      which keeps CPU prompt-processing time bounded.
    - Consecutive same-role turns are merged (multi-chunk bot replies, or a
      follow-up sent after a failed answer).
    """
    turns = [t for t in turns if t.content.strip()]

    used = len(system) + len(seed)
    kept: list[Turn] = []
    for i, t in enumerate(reversed(turns)):
        if i > 0 and used + len(t.content) > budget_chars:
            break
        kept.append(t)
        used += len(t.content)
    kept.reverse()

    merged: list[Turn] = [Turn("user", seed)]
    for t in kept:
        if merged[-1].role == t.role:
            merged[-1] = Turn(t.role, merged[-1].content + "\n\n" + t.content)
        else:
            merged.append(t)

    return [{"role": "system", "content": system}] + [{"role": t.role, "content": t.content} for t in merged]
