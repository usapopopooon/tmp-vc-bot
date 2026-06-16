"""Configuration settings using pydantic-settings.

pydantic-settings を使い、.env ファイルや環境変数から設定値を読み込む。
Bot トークンや DB 接続先など、環境ごとに異なる値をここで一元管理する。
"""

from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.constants import DEFAULT_DATABASE_URL


class Settings(BaseSettings):
    """アプリケーション設定クラス。

    pydantic-settings の BaseSettings を継承し、環境変数や .env ファイルから
    設定値を自動的に読み込む。

    Attributes:
        discord_token (str): Discord Bot のトークン。必須。
        database_url (str): データベース接続 URL。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 必須: Discord Bot トークン
    discord_token: str = ""

    @model_validator(mode="after")
    def validate_required_fields(self) -> "Settings":
        """必須フィールドのバリデーション。"""
        if not self.discord_token or not self.discord_token.strip():
            raise ValueError(
                "DISCORD_TOKEN environment variable is required. "
                "Get your bot token from the Discord Developer Portal: "
                "https://discord.com/developers/applications"
            )
        return self

    # データベース接続 URL。
    # 未指定の場合は POSTGRES_* から組み立てる。
    database_url: str = ""
    postgres_host: str = "db"
    postgres_user: str = "tmp_vc_bot"
    postgres_password: str = ""
    postgres_db: str = "tmp_vc_bot"

    @property
    def async_database_url(self) -> str:
        """DATABASE_URL を非同期ドライバ対応の形式に変換する。

        - postgres://...           → postgresql+asyncpg://... (Heroku 形式)
        - postgresql://...         → postgresql+asyncpg://... (標準形式)
        - postgresql+asyncpg://... → そのまま
        """
        url = self.database_url.strip()
        if not url:
            if self.postgres_password:
                user = quote(self.postgres_user)
                password = quote(self.postgres_password)
                database = quote(self.postgres_db)
                url = (
                    "postgresql+asyncpg://"
                    f"{user}:{password}@{self.postgres_host}:5432/{database}"
                )
            else:
                url = DEFAULT_DATABASE_URL
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


#: アプリケーション全体で共有する設定インスタンス。
settings = Settings()
