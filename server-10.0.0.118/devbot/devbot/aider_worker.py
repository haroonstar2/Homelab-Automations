"""Version-pinned subprocess bridge to Aider; no custom code-generation protocol.

Aider's CLI can return zero after a reported model/edit error. The small API bridge
exposes these errors to the parent publication gate. Verified against 0.86.2.
"""

import importlib.metadata
import json
import sys
from pathlib import Path

if __package__:
    from .workspace_paths import checked_path
else:
    from workspace_paths import checked_path

AIDER_VERSION = "0.86.2"


class PlanningModel:
    """Only the tokenizer-shaped method RepoMap needs; no inference or network."""

    @staticmethod
    def token_count(text):
        # Conservative for source code compared with the usual ~4 chars/token.
        return max(1, len(text) // 3)


def repository_map(payload):
    if importlib.metadata.version("aider-chat") != AIDER_VERSION:
        raise RuntimeError(f"Expected aider-chat=={AIDER_VERSION}; rebuild the runner")
    from aider.io import InputOutput
    from aider.repomap import RepoMap

    root = Path.cwd().resolve()
    artifact_dir = Path(payload["artifact_dir"]).resolve()
    RepoMap.TAGS_CACHE_DIR = str(artifact_dir / "tags-cache")
    requested_files = payload.get("files")
    if not isinstance(requested_files, list) or not all(
        isinstance(name, str) and name for name in requested_files
    ):
        raise ValueError("Repository map requires a filtered list of repository-relative files")
    files = []
    for name in requested_files:
        path = checked_path(root, name)
        if not path.is_file():
            raise ValueError(f"Repository map file does not exist: {name}")
        files.append(str(path))
    mapper = RepoMap(
        map_tokens=payload.get("map_tokens", 4500), root=str(root),
        main_model=PlanningModel(), io=InputOutput(pretty=False, fancy_input=False),
        max_context_window=16384, map_mul_no_files=1,
    )
    result = mapper.get_repo_map([], files, mentioned_fnames=set(payload.get("mentioned_fnames", []))) or ""
    Path(payload["output_file"]).write_text(result, encoding="utf-8")
    mapper.TAGS_CACHE.close()


def make_coder(payload):
    if importlib.metadata.version("aider-chat") != AIDER_VERSION:
        raise RuntimeError(f"Expected aider-chat=={AIDER_VERSION}; rebuild the runner")
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model, model_info_manager
    from aider.repomap import RepoMap

    # The worker is a separate process; move Aider's cache outside the worktree.
    artifact_dir = Path(payload["artifact_dir"]).resolve()
    RepoMap.TAGS_CACHE_DIR = str(artifact_dir / "tags-cache")
    root = Path.cwd().resolve()
    files = [str(checked_path(root, name)) for name in payload["files"]]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    io = InputOutput(
        yes=True, pretty=False, fancy_input=False,
        llm_history_file=str(artifact_dir / "llm-history.md"),
    )
    original_read = io.read_text
    original_write = io.write_text

    def read_text(filename, *args, **kwargs):
        return original_read(checked_path(root, str(filename), absolute=True), *args, **kwargs)

    def write_text(filename, *args, **kwargs):
        return original_write(checked_path(root, str(filename), absolute=True), *args, **kwargs)

    io.read_text = read_text
    io.write_text = write_text
    context_tokens = int(payload["context_tokens"])
    output_tokens = int(payload["output_tokens"])
    input_tokens = context_tokens - output_tokens
    model_name = "openai/" + payload["model"]
    model_info_manager.local_model_metadata[model_name] = {
        "max_tokens": context_tokens, "max_input_tokens": input_tokens,
        "max_output_tokens": output_tokens, "litellm_provider": "openai",
    }
    model = Model(model_name, weak_model=model_name, editor_model=model_name)
    model.extra_params = {**(model.extra_params or {}), "max_tokens": output_tokens}
    coder = Coder.create(
        main_model=model, io=io, fnames=files, edit_format="diff",
        auto_commits=False, dirty_commits=False, stream=False,
        auto_lint=False, auto_test=False, suggest_shell_commands=False,
        detect_urls=False, map_tokens=2048,
    )
    # The outer runner owns a separate verification/repair pass. One Aider
    # reflection is enough to correct a malformed diff without multiplying
    # slow local-model calls by the library default of three.
    coder.max_reflections = 1
    def strict_token_check(messages):
        estimated = model.token_count(messages)
        if estimated is None or estimated >= input_tokens:
            io.tool_error(
                f"Estimated input {estimated} does not fit the configured {input_tokens}-token input budget"
            )
            return False
        return True

    coder.check_tokens = strict_token_check
    original_get_edits = coder.get_edits

    def get_edits():
        edits = original_get_edits()
        # Validate the entire response before Aider's dry-run can touch any paths.
        for name, *_ in edits:
            checked_path(root, name)
        return edits

    coder.get_edits = get_edits
    return coder


def run(payload):
    coder = make_coder(payload)
    errors = []
    finish_reasons = []
    original_error = coder.io.tool_error
    original_show = getattr(coder, "show_send_output", None)

    def record_error(*args, **kwargs):
        errors.append(str(args[0]) if args else "Aider error")
        return original_error(*args, **kwargs)

    coder.io.tool_error = record_error
    if original_show:
        def record_finish_reason(completion):
            choices = getattr(completion, "choices", None) or []
            finish_reasons.append(getattr(choices[0], "finish_reason", None) if choices else None)
            return original_show(completion)

        coder.show_send_output = record_finish_reason
    coder.run(with_message=payload["message"])
    if errors or coder.num_exhausted_context_windows or coder.num_malformed_responses:
        raise RuntimeError("Aider reported incomplete generation or an edit error; inspect the run log")
    Path(payload["result_file"]).write_text(json.dumps({
        "aider_version": AIDER_VERSION,
        "model": coder.main_model.name,
        "model_info": coder.main_model.info,
        "tokens_sent": coder.total_tokens_sent,
        "tokens_received": coder.total_tokens_received,
        "finish_reasons": finish_reasons,
        "malformed_responses": coder.num_malformed_responses,
        "exhausted_context_windows": coder.num_exhausted_context_windows,
    }, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if request.get("action") == "repository_map":
        repository_map(request)
    else:
        run(request)
