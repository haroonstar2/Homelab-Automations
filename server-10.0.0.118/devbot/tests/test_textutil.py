from pathlib import Path
from devbot.docs_rag import chunk_text
from devbot.github import parse_pr_reference, slugify_branch
from devbot.notion import blocks_to_markdown, markdown_to_blocks, normalize_language
from devbot.runner import extract_package_symbols, score_file_relevance
from devbot.textutil import Turn, build_messages, split_message, strip_think


# --- strip_think -----------------------------------------------------------

def test_strip_think_removes_blocks():
    assert strip_think("<think>hmm\nok</think>\nAnswer") == "Answer"


def test_strip_think_drops_unterminated_block():
    assert strip_think("Answer <think>still thinking") == "Answer"


# --- split_message ---------------------------------------------------------

def test_short_message_is_single_chunk():
    assert split_message("hello") == ["hello"]


def test_empty_message():
    assert split_message("   ") == []


def test_chunks_respect_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_code_fence_is_closed_and_reopened():
    code = "\n".join(f"    x_{i} = {i}" for i in range(100))
    text = f"Intro\n```python\n{code}\n```\nOutro"
    chunks = split_message(text, limit=300)
    assert len(chunks) > 2
    for c in chunks:
        assert len(c) <= 300
        assert c.count("```") % 2 == 0, c  # every chunk has balanced fences
    assert chunks[1].startswith("```python")


def test_very_long_line_is_hard_split():
    text = "a" * 1234
    chunks = split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


# --- build_messages --------------------------------------------------------

def test_build_messages_basic_shape():
    msgs = build_messages("SYS", "Q", [Turn("assistant", "A1"), Turn("user", "Q2")])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "Q"


def test_build_messages_merges_consecutive_roles():
    msgs = build_messages("S", "Q", [Turn("assistant", "part1"), Turn("assistant", "part2"), Turn("user", "next")])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "part1" in msgs[2]["content"] and "part2" in msgs[2]["content"]


def test_followup_after_failed_answer_is_kept():
    msgs = build_messages("S", "Q", [Turn("user", "retry please")])
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "retry please" in msgs[1]["content"]


def test_budget_drops_oldest_but_keeps_latest():
    turns = [Turn("assistant", "old" * 100), Turn("user", "mid" * 100), Turn("assistant", "a"), Turn("user", "latest" * 1000)]
    msgs = build_messages("S", "Q", turns, budget_chars=100)
    contents = " ".join(m["content"] for m in msgs)
    assert "latest" in contents
    assert "old" not in contents
    assert msgs[1]["content"].startswith("Q")


# --- notion markdown <-> blocks --------------------------------------------

def test_notion_markdown_roundtrip():
    md = "# Title\n\n## Section\n\n- bullet one\n- bullet two\n\n```py\nprint('hi')\n```\n\n---\n\nParagraph text."
    blocks = markdown_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert types == ["heading_1", "heading_2", "bulleted_list_item", "bulleted_list_item", "code", "divider", "paragraph"]
    assert blocks[4]["code"]["language"] == "python"
    back = blocks_to_markdown(blocks)
    assert "# Title" in back and "```python" in back and "- bullet one" in back


def test_normalize_language():
    assert normalize_language("py") == "python"
    assert normalize_language("ts") == "typescript"
    assert normalize_language("unknown-lang") == "plain text"


# --- docs_rag chunking -----------------------------------------------------

def test_chunk_text_splits_with_overlap():
    text = "\n\n".join(f"Paragraph {i}: " + ("word " * 40) for i in range(20))
    chunks = chunk_text(text, chunk_size=600, overlap=100)
    assert len(chunks) > 4
    assert all(len(c) <= 600 for c in chunks)
    assert "Paragraph 0:" in chunks[0]
    assert "Paragraph 19:" in chunks[-1]


# --- github helpers --------------------------------------------------------

def test_parse_pr_reference():
    assert parse_pr_reference("https://github.com/haroonstar2/IDKServer/pull/42") == ("haroonstar2/IDKServer", 42)
    assert parse_pr_reference("haroonstar2/IDKServer#7") == ("haroonstar2/IDKServer", 7)
    assert parse_pr_reference("IDKServer#9", default_owner="haroonstar2") == ("haroonstar2/IDKServer", 9)
    assert parse_pr_reference("not-a-pr") is None


def test_slugify_branch():
    assert slugify_branch("Add Redis rate limiter!") == "agent/add-redis-rate-limiter"


def test_score_file_relevance_ranks_target_files_above_generic():
    prompt = (
        "Go Game Logic (apps/game-server/internal/game): room.go, room_test.go\n"
        "WebSocket Router (apps/game-server/internal/ws/hub.go)\n"
        "Frontend Wiring (GameActionBar & GamePage)"
    )
    assert score_file_relevance(Path("apps/game-server/internal/ws/hub.go"), prompt) == 0
    assert score_file_relevance(Path("apps/web-client/components/GameActionBar.tsx"), prompt) == 1
    assert score_file_relevance(Path("apps/web-client/app/game/[id]/page.tsx"), prompt) == 1
    assert score_file_relevance(Path("apps/game-server/internal/game/messages.go"), prompt) == 2
    assert score_file_relevance(Path("package.json"), prompt) == 99
    assert score_file_relevance(Path("apps/core-api/src/main.ts"), prompt) == 99


def test_extract_package_symbols_catches_go_methods_and_consts():
    siblings = {
        "internal/game/completion.go": (
            "package game\n"
            "type CompletionEvent struct {\n  GameID string\n}\n"
            "func (r *GameRoom) Resign(playerID string) error {\n  return nil\n}\n"
            "func (r *GameRoom) completeLocked(reason string) {\n}\n"
        ),
        "internal/game/messages.go": (
            "package game\n"
            "const (\n"
            '  TypeGameState = "GAME_STATE"\n'
            '  TypeMove = "MOVE"\n'
            ")\n"
        ),
    }
    idx = extract_package_symbols(siblings, exclude_rel="internal/game/messages.go")
    assert "func (r *GameRoom) Resign(playerID string) error" in idx
    assert "func (r *GameRoom) completeLocked(reason string)" in idx
    assert "type CompletionEvent struct" in idx
    assert "TypeGameState" not in idx  # excluded target file
