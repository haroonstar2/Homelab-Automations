#!/usr/bin/env python3
"""Measure candidate Ollama model throughput (not code correctness).

Run on 10.0.0.118 (stdlib only, no installs needed):
    python3 benchmark_models.py
    python3 benchmark_models.py qwen3-coder:30b qwen2.5-coder:14b

Each model must already be pulled:  docker exec ollama ollama pull <model>
Models are unloaded after testing so the next one has the RAM to itself.
Use evaluate_code_models.py before selecting a production model; speed alone is
not a quality signal.
"""

import json
import sys
import time
import urllib.request

OLLAMA = "http://localhost:11434"
DEFAULT_MODELS = ["qwen3-coder:30b", "qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen3:4b"]

SHORT_PROMPT = (
    "Write a Python function `retry(fn, attempts, base_delay)` that retries a callable with "
    "exponential backoff and jitter. Include type hints and a short docstring."
)

# ~4k tokens of context, roughly what /ask with pasted docs or /code with a repo map looks like.
_SNIPPET = '''
class RateLimiter:
    """Token bucket rate limiter backed by Redis."""
    def __init__(self, redis, key: str, capacity: int, refill_per_sec: float) -> None:
        self.redis, self.key, self.capacity, self.refill = redis, key, capacity, refill_per_sec
    async def allow(self, cost: int = 1) -> bool:
        now = time.monotonic()
        tokens, last = await self.redis.hmget(self.key, "tokens", "last")
        tokens = min(self.capacity, float(tokens or self.capacity) + (now - float(last or now)) * self.refill)
        if tokens < cost:
            return False
        await self.redis.hset(self.key, mapping={"tokens": tokens - cost, "last": now})
        return True
'''
LONG_PROMPT = "Here is some code from my project:\n" + (_SNIPPET * 22) + (
    "\nThe allow() method has a race condition under concurrent callers. Explain it briefly "
    "and show a fixed version using a Lua script."
)


def call(model: str, prompt: str, num_predict: int, keep_alive: str = "10m") -> dict:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": 16384, "num_predict": num_predict, "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.load(resp)


def rate(count: int, duration_ns: int) -> float:
    return count / (duration_ns / 1e9) if duration_ns else 0.0


def bench(model: str) -> dict | None:
    print(f"\n=== {model} ===", flush=True)
    try:
        t0 = time.time()
        warm = call(model, "Say OK.", 4)
        print(f"  load + warmup: {time.time() - t0:.1f}s", flush=True)

        short = call(model, SHORT_PROMPT, 256)
        gen_tps = rate(short["eval_count"], short["eval_duration"])
        print(f"  generation:        {gen_tps:6.1f} tok/s  ({short['eval_count']} tokens)", flush=True)

        long = call(model, LONG_PROMPT, 64, keep_alive="0")  # unload afterwards
        pp_tps = rate(long["prompt_eval_count"], long["prompt_eval_duration"])
        print(f"  prompt processing: {pp_tps:6.1f} tok/s  ({long['prompt_eval_count']} prompt tokens)", flush=True)
    except Exception as exc:
        print(f"  FAILED: {exc}  (is it pulled? docker exec ollama ollama pull {model})")
        return None

    # Typical /ask follow-up: ~4k tokens in, ~500 out. Typical /code step: ~10k in, ~800 out.
    ask_s = 4000 / pp_tps + 500 / gen_tps if pp_tps and gen_tps else float("inf")
    code_s = 10000 / pp_tps + 800 / gen_tps if pp_tps and gen_tps else float("inf")
    return {"model": model, "gen": gen_tps, "pp": pp_tps, "ask": ask_s, "code": code_s, "_": warm}


def main() -> None:
    models = sys.argv[1:] or DEFAULT_MODELS
    results = [r for r in (bench(m) for m in models) if r]
    if not results:
        return
    print("\n" + "=" * 78)
    print(f"{'model':<22}{'gen tok/s':>11}{'prompt tok/s':>14}{'~/ask reply':>14}{'~/code step':>14}")
    for r in results:
        print(f"{r['model']:<22}{r['gen']:>11.1f}{r['pp']:>14.1f}{r['ask'] / 60:>12.1f}m{r['code'] / 60:>13.1f}m")
    print("\nThis is performance data only. Run evaluate_code_models.py and require all")
    print("correctness cases to pass before setting a model as `smart` in litellm/config.yaml.")


if __name__ == "__main__":
    main()
