"""Tests for runtime settings."""

import pytest
from pydantic import ValidationError

from src.config import Settings


def test_single_discord_token_populates_tokens() -> None:
    settings = Settings(discord_token="abc", discord_tokens=[])

    assert settings.discord_tokens == ["abc"]
    assert settings.discord_token == "abc"


def test_discord_tokens_csv_can_drive_config() -> None:
    settings = Settings(discord_token="", discord_tokens="a, b ,c")

    assert settings.discord_tokens == ["a", "b", "c"]
    assert settings.discord_token == "a"


def test_discord_token_and_tokens_merge_with_dedup() -> None:
    settings = Settings(discord_token="a", discord_tokens="b,a,c")

    assert settings.discord_tokens == ["b", "a", "c"]
    assert settings.discord_token == "b"


def test_requires_at_least_one_discord_token() -> None:
    with pytest.raises(ValidationError, match="DISCORD_TOKEN or DISCORD_TOKENS"):
        Settings(discord_token="", discord_tokens=[])
