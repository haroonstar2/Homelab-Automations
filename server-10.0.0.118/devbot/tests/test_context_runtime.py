import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from devbot.runner import CodeRunner


class RuntimeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_context_is_recorded_and_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodeRunner(Path(tmp), "", "", "", ollama_url="http://ollama", ollama_model="model")
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = lambda: {"models": [{"name": "model", "context_length": 16384}]}
            client = AsyncMock()
            client.get.return_value = response
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            with patch("httpx.AsyncClient", return_value=client):
                await runner._record_runtime_context(Path(tmp))
            self.assertEqual(
                json.loads((Path(tmp) / "artifacts" / "ollama-runtime.json").read_text())["context_length"],
                16384,
            )

            response.json = lambda: {"models": [{"name": "model", "context_length": 4096}]}
            with patch("httpx.AsyncClient", return_value=client), self.assertRaises(RuntimeError):
                await runner._record_runtime_context(Path(tmp))
