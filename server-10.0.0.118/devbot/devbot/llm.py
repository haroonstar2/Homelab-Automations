"""Thin client for the LiteLLM proxy. devbot only refers to model aliases."""

import logging

from openai import AsyncOpenAI

from .textutil import strip_think

log = logging.getLogger(__name__)

SMART = "smart"  # architecture / code questions
FAST = "fast"    # check-ins, summaries, classification
EMBED = "embed"  # document embeddings (nomic-embed-text, 768 dims)


class IncompleteModelResponse(RuntimeError):
    """The provider stopped before producing an ordinary complete text response."""


class LLM:
    def __init__(self, base_url: str, api_key: str) -> None:
        # CPU inference: prompt processing alone can take minutes on long contexts.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=1800, max_retries=0)

    async def chat(self, model: str, messages: list[dict[str, str]], max_tokens: int = 1500) -> str:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        if not resp.choices:
            raise IncompleteModelResponse("Model response contained no choices")
        choice = resp.choices[0]
        if choice.finish_reason != "stop":
            raise IncompleteModelResponse(
                f"Model response was incomplete (finish_reason={choice.finish_reason!r})"
            )
        text = choice.message.content or ""
        if not text.strip():
            raise IncompleteModelResponse("Model returned an empty text response")
        usage = resp.usage
        if usage:
            log.info("llm %s: prompt=%s completion=%s tokens", model, usage.prompt_tokens, usage.completion_tokens)
        return strip_think(text)

    async def embed(self, texts: list[str], model: str = EMBED) -> list[list[float]]:
        """Generate vector embeddings for one or more text chunks."""
        if not texts:
            return []
        resp = await self._client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]

    async def models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)
