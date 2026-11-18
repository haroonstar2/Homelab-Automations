# Model qualification

Run this only from the dedicated agent identity after OpenCode 2.2.7 and its
LiteLLM provider configuration are installed:

```bash
export LITELLM_API_KEY='loaded from the mode-0600 server file'
python3 tests/model-qualification/qualify.py --model litellm/local-code
```

Both automated cases must pass and their logs must be retained. Then manually
run and record the remaining policy cases: long-context retention, explicit
context-limit failure, truncated output, failed push/PR truthfulness, task
continuation, and cancellation. Do not use an LLM judge as the sole pass signal.

Qualification is per exact model tag/digest and per exact Ollama, LiteLLM, and
OpenCode versions. Any of those changing invalidates the record.

