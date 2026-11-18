"""Durable, append-only owner feedback, independent of short model-written plans."""

import json
from pathlib import Path


def append_feedback(data_dir: Path, thread_id: int, message_id: int, content: str) -> None:
    path = data_dir / "task-context" / f"{thread_id}.json"
    events = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(event["message_id"] == message_id for event in events):
        return
    events.append({"message_id": message_id, "content": content})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def task_specification(data_dir: Path, thread_id: int, original_task: str) -> str:
    path = data_dir / "task-context" / f"{thread_id}.json"
    if not path.exists():
        return original_task
    events = json.loads(path.read_text(encoding="utf-8"))
    updates = "\n\n".join(f"Owner update {n}:\n{e['content']}" for n, e in enumerate(events, 1))
    return (f"Original task:\n{original_task}\n\nOwner updates (chronological; later explicit corrections "
            f"supersede earlier instructions; retain other requirements):\n{updates}")
