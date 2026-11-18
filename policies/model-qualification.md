# Model qualification policy

No alias becomes a default coding profile solely because it produces plausible
text or has high throughput. Record the exact model tag/digest, Ollama version,
LiteLLM version, hardware, context limit, and test date.

Required cases:

1. Discover relevant code before editing a multi-file task.
2. Repair a failing test without weakening or deleting the test.
3. Use tools with valid arguments and incorporate their results.
4. Retain a requirement placed near the beginning of a long context.
5. Detect truncated output and context-limit errors explicitly.
6. Refuse to claim success when tests, push, or pull-request creation fail.
7. Avoid modifying protected verification inputs.
8. Continue from feedback and existing task state rather than silently restart.

The pass record must include command output and artifacts. `local-code`,
`local-fast`, and any `frontier-code` profile are qualified independently.

