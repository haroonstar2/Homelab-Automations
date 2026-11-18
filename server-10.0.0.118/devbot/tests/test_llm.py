import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from devbot.llm import IncompleteModelResponse, LLM


def response(finish_reason="stop", content="complete", choices=True):
    rows = []
    if choices:
        rows.append(SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content),
        ))
    return SimpleNamespace(choices=rows, usage=None)


class LLMCompletionTests(unittest.IsolatedAsyncioTestCase):
    def client(self, value):
        create = AsyncMock(return_value=value)
        llm = LLM.__new__(LLM)
        llm._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        return llm

    async def test_stop_with_text_is_returned(self):
        self.assertEqual(
            await self.client(response()).chat("smart", [{"role": "user", "content": "hi"}]),
            "complete",
        )

    async def test_incomplete_finish_reasons_are_rejected(self):
        for reason in ("length", "content_filter", "tool_calls", "function_call", None):
            with self.subTest(reason=reason):
                with self.assertRaises(IncompleteModelResponse):
                    await self.client(response(finish_reason=reason)).chat(
                        "smart", [{"role": "user", "content": "hi"}],
                    )

    async def test_missing_choice_or_empty_text_is_rejected(self):
        with self.assertRaises(IncompleteModelResponse):
            await self.client(response(choices=False)).chat(
                "smart", [{"role": "user", "content": "hi"}],
            )
        with self.assertRaises(IncompleteModelResponse):
            await self.client(response(content="  ")).chat(
                "smart", [{"role": "user", "content": "hi"}],
            )


if __name__ == "__main__":
    unittest.main()
