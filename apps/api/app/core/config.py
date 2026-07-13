from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MangaFinder"
    database_url: str = "sqlite:///./data/mangafinder.db"
    cors_origins: str = "http://localhost:5173"
    poll_interval_seconds: float = 2.0
    max_job_attempts: int = 3
    worker_enabled: bool = True
    downloads_dir: Path = Path("./data/downloads")
    use_data_saver: bool = True
    chapter_languages: str = "zh-hans,zh-hant,en,ja"
    wnacg_base_urls: str = (
        "https://www.wnacg.com,https://www.wn08.cfd,"
        "https://www.wn07.cfd,https://www.wn08.shop,https://www.wn07.shop"
    )
    wnacg_cookie: str = ""
    wnacg_max_search_pages: int = 5
    nhentai_enabled: bool = True
    nhentai_base_url: str = "https://nhentai.net"
    nhentai_proxy_url: str = ""
    nhentai_cookie: str = ""
    nhentai_max_search_pages: int = 3
    agent_enabled: bool = False
    agent_provider: str = "openai_compatible"
    agent_base_url: str = "http://host.docker.internal:11434/v1"
    agent_model: str = ""
    agent_api_key: str = ""
    agent_temperature: float = 0.0
    agent_timeout_seconds: float = 60.0
    agent_max_reviews_per_run: int = 20
    agent_review_after_discovery: bool = False
    agent_auto_apply: bool = False
    agent_auto_apply_threshold: float = 0.98
    agent_allow_cloud_images: bool = False
    agent_prompt_version: str = "v6"
    static_dir: Path = Path(__file__).resolve().parents[3] / "web" / "dist"
    user_agent: str = "MangaFinder/0.1 (+https://github.com/local/mangafinder)"

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MANGAFINDER_", extra="ignore"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def chapter_language_list(self) -> list[str]:
        return [item.strip() for item in self.chapter_languages.split(",") if item.strip()]

    @property
    def wnacg_base_url_list(self) -> list[str]:
        return [
            item.strip().rstrip("/")
            for item in self.wnacg_base_urls.split(",")
            if item.strip()
        ]

    @property
    def agent_configured(self) -> bool:
        return bool(self.agent_enabled and self.agent_model.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
