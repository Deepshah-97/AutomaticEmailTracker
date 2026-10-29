from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://agent:agent@localhost:5432/followup"
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"
    # Restrict Gmail connections to the company's Workspace domain(s), comma separated.
    google_allowed_domains: str = ""
    token_encryption_key: str = ""
    admin_api_key: str = ""
    company_name: str = "the company"
    sentry_dsn: str = ""
    default_send_mode: str = "approve"
    scheduler_enabled: bool = False
    # Master switch: no email is ever sent unless the operator turns this on (fail closed).
    sending_enabled: bool = False

    # Demo isolation (A7). TEST_MODE may skip waits, send windows and random delay, and only for
    # allowlisted sender and recipient addresses. It never bypasses stop conditions or approval.
    test_mode: bool = False
    test_allowlist: str = ""

    send_window_start: str = "08:30"
    send_window_end: str = "17:30"
    random_delay_max_minutes: int = 40
    max_sends_per_user_per_day: int = 50
    rate_limit_per_minute: int = 60
    claim_lease_seconds: int = 300
    webhook_max_attempts: int = 6  # first try + 5 retries
    ai_daily_alert_usd: float = 2.0
    send_failure_alert_per_hour: int = 3
    followup_retention_days: int = 180
    thread_token_budget: int = 12000

    @property
    def allowlist(self) -> set[str]:
        return {e.strip().lower() for e in self.test_allowlist.split(",") if e.strip()}

    @property
    def allowed_domains(self) -> set[str]:
        return {d.strip().lower() for d in self.google_allowed_domains.split(",") if d.strip()}

    def test_mode_allows(self, *addresses: str) -> bool:
        """True when TEST_MODE is on and every address is on the allowlist."""
        return self.test_mode and bool(self.allowlist) and all(a.lower() in self.allowlist for a in addresses)


@lru_cache
def get_settings() -> Settings:
    return Settings()
