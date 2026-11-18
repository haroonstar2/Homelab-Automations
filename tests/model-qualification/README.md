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

If a harness defect invalidates only one case after another case has passed on
the exact same pinned model and runtime versions, rerun only that case with:

```bash
sudo scripts/run-model-qualification.sh --apply --case CASE_NAME
```

Retain both reports; never use a single-case run to conceal a valid failure.

Qualification is per exact model tag/digest and per exact Ollama, LiteLLM, and
OpenCode versions. Any of those changing invalidates the record.

