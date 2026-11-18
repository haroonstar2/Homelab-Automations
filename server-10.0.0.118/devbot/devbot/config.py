import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    discord_token: str
    owner_id: int          # only this Discord user can use the bot
    guild_id: int          # slash commands are synced to this server (instant updates)
    channel_id: int        # #general: scheduled posts go here
    llm_base_url: str      # LiteLLM proxy, OpenAI-compatible
    llm_api_key: str
    database_url: str
    data_dir: Path
    timezone: str
    notion_token: str
    notion_root_page_id: str
    github_token: str
    searxng_url: str
    llm_context_tokens: int
    llm_output_tokens: int
    ollama_url: str
    ollama_model: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            owner_id=int(_required("DISCORD_OWNER_ID")),
            guild_id=int(_required("DISCORD_GUILD_ID")),
            channel_id=int(_required("DISCORD_CHANNEL_ID")),
            llm_base_url=os.environ.get("LLM_BASE_URL", "http://litellm:4000/v1"),
            llm_api_key=_required("LITELLM_MASTER_KEY"),
            database_url=_required("DATABASE_URL"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            timezone=os.environ.get("TZ", "America/Los_Angeles"),
            notion_token=os.environ.get("NOTION_TOKEN", "").strip(),
            notion_root_page_id=os.environ.get("NOTION_ROOT_PAGE_ID", "").strip(),
            github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
            searxng_url=os.environ.get("SEARXNG_URL", "http://searxng:8080").strip(),
            llm_context_tokens=int(os.environ.get("LLM_CONTEXT_TOKENS", "16384")),
            llm_output_tokens=int(os.environ.get("LLM_OUTPUT_TOKENS", "4096")),
            ollama_url=os.environ.get("OLLAMA_URL", "").rstrip("/"),
            ollama_model=os.environ.get("OLLAMA_MODEL", "").strip(),
        )
