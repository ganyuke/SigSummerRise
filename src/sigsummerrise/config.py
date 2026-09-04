from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    signal_account: str = ""
    signal_group_id: str = ""
    signal_bot_aci: str = ""
    signal_http_url: str = "http://127.0.0.1:8080"
    public_base_url: str = "http://127.0.0.1:8000"
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-v4-flash-0731"
    max_n: int = 200
    ask_context_n: int = 50
    db_path: str = "data/sigsummerrise.db"
    db_key: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    magic_token_ttl_seconds: int = 900
    session_ttl_seconds: int = 86400
    dashboard_links_per_hour: int = 3
    llm_calls_per_hour: int = 10
    llm_queue_cap: int = 3
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    llm_timeout_seconds: int = 600
    llm_read_idle_seconds: int = 90
    operator_token: str = ""
    responses_path: str = "copy/responses.json"
    prompts_path: str = "copy/prompts.json"
    bot_name: str = "SigSummerRise"
    bot_timezone: str = "UTC"
    group_name: str = ""

    @property
    def public_origin(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def cookie_secure(self) -> bool:
        return self.public_origin.lower().startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        if self.cookie_secure:
            return "__Host-ssr_session"
        return "ssr_session"
