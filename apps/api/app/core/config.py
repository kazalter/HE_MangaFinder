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
    cover_cache_dir: Path = Path("./data/cover-cache")
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
    nhentai_max_search_pages: int = 10
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
    agent_prompt_version: str = "v8"
    social_enabled: bool = False
    social_collector_base_url: str = "http://social-collector:8010"
    social_collector_token: str = ""
    social_sync_interval_minutes: int = 120
    social_event_sync_interval_minutes: int = 30
    social_initial_backfill_days: int = 90
    social_max_posts_per_sync: int = 100
    social_agent_enabled: bool = True
    social_agent_prompt_version: str = "social-zh-v2"
    social_auto_confirm_threshold: float = 0.92
    social_candidate_threshold: float = 0.60
    social_media_dir: Path = Path("./data/social-media")
    social_ocr_enabled: bool = True
    social_ocr_max_posts_per_sync: int = 12
    social_ocr_timeout_seconds: float = 30.0
    social_daily_digest_enabled: bool = True
    social_daily_digest_hour: int = 20
    social_daily_digest_timezone: str = "Asia/Shanghai"
    social_daily_digest_initial_lookback_days: int = 7
    social_daily_digest_min_importance: str = "normal"
    social_daily_digest_max_authors: int = 20
    social_daily_digest_max_items_per_author: int = 3
    public_base_url: str = "http://localhost:8000"
    qq_bot_enabled: bool = False
    qq_bot_app_id: str = ""
    qq_bot_client_secret: str = ""
    qq_bot_user_openid: str = ""
    qq_bot_api_base_url: str = "https://api.sgroup.qq.com"
    qq_bot_token_url: str = "https://bots.qq.com/app/getAppAccessToken"
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

    @property
    def social_agent_configured(self) -> bool:
        return bool(self.social_agent_enabled and self.agent_configured)

    @property
    def qq_bot_configured(self) -> bool:
        return bool(
            self.qq_bot_enabled
            and self.qq_bot_app_id.strip()
            and self.qq_bot_client_secret.strip()
            and self.qq_bot_user_openid.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
