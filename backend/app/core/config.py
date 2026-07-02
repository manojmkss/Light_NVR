from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Security
    # Blank by default - auto-generated and persisted on first boot (see
    # app.core.bootstrap.ensure_jwt_secret) rather than requiring the user to
    # invent and paste in a random string before first run. An explicit env
    # var still overrides this, for scripted/multi-instance deployments.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    # Fallback defaults only - overwritten from the DB-backed SecuritySettings
    # row at startup (see app.core.bootstrap.load_security_settings), which
    # is what Settings -> Account actually edits.
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7

    # Optional unattended bootstrap for scripted/headless deployments - if
    # either is blank (the default), no admin is auto-created and the GUI
    # setup wizard handles it on first visit instead. Shipping a guessable
    # default admin/admin account is not acceptable for a public release.
    admin_username: str = ""
    admin_password: str = ""

    # Database (infra-level; not exposed in the GUI - see README)
    database_url: str = "sqlite+aiosqlite:////data/lightnvr.db"


settings = Settings()
