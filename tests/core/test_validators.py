"""Tests for core validators."""

import pytest

from src.core.validators import (
    validate_bitrate,
    validate_channel_name,
    validate_user_limit,
)


class TestValidateUserLimit:
    """Tests for validate_user_limit function."""

    def test_valid_limit(self) -> None:
        """Test valid user limits."""
        assert validate_user_limit(10) is True

    def test_zero_is_valid(self) -> None:
        """Test that 0 (unlimited) is valid."""
        assert validate_user_limit(0) is True

    def test_max_limit_is_valid(self) -> None:
        """Test that 99 is valid."""
        assert validate_user_limit(99) is True

    def test_negative_is_invalid(self) -> None:
        """Test that negative values are invalid."""
        assert validate_user_limit(-1) is False

    def test_over_99_is_invalid(self) -> None:
        """Test that values over 99 are invalid."""
        assert validate_user_limit(100) is False


class TestValidateChannelName:
    """Tests for validate_channel_name function."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("valid-name", True),
            ("a", True),
            ("a" * 100, True),
            ("", False),
            ("a" * 101, False),
        ],
    )
    def test_channel_name_validation(self, name: str, expected: bool) -> None:
        """Test channel name validation with various inputs."""
        assert validate_channel_name(name) is expected


class TestValidateBitrate:
    """Tests for validate_bitrate function."""

    def test_valid_bitrate(self) -> None:
        """Test valid bitrates."""
        assert validate_bitrate(64) is True
        assert validate_bitrate(128) is True

    def test_min_bitrate(self) -> None:
        """Test minimum bitrate."""
        assert validate_bitrate(8) is True
        assert validate_bitrate(7) is False

    def test_max_bitrate(self) -> None:
        """Test maximum bitrate."""
        assert validate_bitrate(384) is True
        assert validate_bitrate(385) is False
