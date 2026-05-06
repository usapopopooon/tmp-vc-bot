"""Tests for core permissions."""

from unittest.mock import MagicMock

import discord

from src.core.permissions import (
    build_locked_overwrites,
    build_unlocked_overwrites,
    is_owner,
)


def _make_guild_with_members(
    member_ids: list[int],
) -> tuple[MagicMock, dict[int, MagicMock]]:
    """Create a mock guild with members."""
    members: dict[int, MagicMock] = {}
    for mid in member_ids:
        m = MagicMock(spec=discord.Member)
        m.id = mid
        members[mid] = m

    guild = MagicMock(spec=discord.Guild)
    guild.default_role = MagicMock(spec=discord.Role)
    guild.get_member = lambda uid: members.get(uid)
    return guild, members


class TestIsOwner:
    """Tests for is_owner function."""

    def test_owner_matches(self) -> None:
        """Test when user is the owner."""
        assert is_owner("123456789", 123456789) is True

    def test_owner_does_not_match(self) -> None:
        """Test when user is not the owner."""
        assert is_owner("123456789", 987654321) is False

    def test_owner_id_as_string(self) -> None:
        """Test that owner_id is correctly compared as string."""
        assert is_owner("123", 123) is True
        assert is_owner("0123", 123) is False


class TestBuildLockedOverwrites:
    """Tests for build_locked_overwrites function."""

    def test_default_role_denied_connect(self) -> None:
        """Test that @everyone is denied connect."""
        guild, _ = _make_guild_with_members([100])
        result = build_locked_overwrites(guild, 100)
        overwrite = result[guild.default_role]
        assert overwrite.connect is False

    def test_owner_has_full_permissions(self) -> None:
        """Test that owner gets full permissions."""
        guild, members = _make_guild_with_members([100])
        result = build_locked_overwrites(guild, 100)
        ow = result[members[100]]
        assert ow.connect is True
        assert ow.speak is True
        assert ow.stream is True
        assert ow.move_members is True
        assert ow.mute_members is True
        assert ow.deafen_members is True

    def test_owner_not_in_guild(self) -> None:
        """Test when owner is not found in guild."""
        guild, _ = _make_guild_with_members([])
        result = build_locked_overwrites(guild, 999)
        # Only default_role should be present
        assert len(result) == 1
        assert guild.default_role in result

    def test_allowed_users_get_connect(self) -> None:
        """Test that allowed users get connect permission."""
        guild, members = _make_guild_with_members([100, 200, 300])
        result = build_locked_overwrites(guild, 100, [200, 300])
        assert result[members[200]].connect is True
        assert result[members[300]].connect is True

    def test_allowed_users_not_in_guild_ignored(self) -> None:
        """Test that non-existent allowed users are ignored."""
        guild, members = _make_guild_with_members([100])
        result = build_locked_overwrites(guild, 100, [999])
        # Only default_role + owner
        assert len(result) == 2
        assert members[100] in result

    def test_no_allowed_users(self) -> None:
        """Test with no allowed users list."""
        guild, members = _make_guild_with_members([100])
        result = build_locked_overwrites(guild, 100)
        assert len(result) == 2  # default_role + owner


class TestBuildUnlockedOverwrites:
    """Tests for build_unlocked_overwrites function."""

    def test_no_default_role_overwrite(self) -> None:
        """Test that @everyone has no overwrite."""
        guild, _ = _make_guild_with_members([100])
        result = build_unlocked_overwrites(guild, 100)
        assert guild.default_role not in result

    def test_owner_has_moderation_permissions(self) -> None:
        """Test that owner gets moderation permissions."""
        guild, members = _make_guild_with_members([100])
        result = build_unlocked_overwrites(guild, 100)
        ow = result[members[100]]
        assert ow.connect is True
        assert ow.speak is True
        assert ow.stream is True
        assert ow.move_members is True
        assert ow.mute_members is True
        assert ow.deafen_members is True

    def test_owner_not_in_guild(self) -> None:
        """Test when owner is not found in guild."""
        guild, _ = _make_guild_with_members([])
        result = build_unlocked_overwrites(guild, 999)
        assert len(result) == 0

    def test_blocked_users_denied_connect(self) -> None:
        """Test that blocked users are denied connect."""
        guild, members = _make_guild_with_members([100, 200, 300])
        result = build_unlocked_overwrites(guild, 100, [200, 300])
        assert result[members[200]].connect is False
        assert result[members[300]].connect is False

    def test_blocked_users_not_in_guild_ignored(self) -> None:
        """Test that non-existent blocked users are ignored."""
        guild, members = _make_guild_with_members([100])
        result = build_unlocked_overwrites(guild, 100, [999])
        # Only owner
        assert len(result) == 1
        assert members[100] in result

    def test_no_blocked_users(self) -> None:
        """Test with no blocked users list."""
        guild, members = _make_guild_with_members([100])
        result = build_unlocked_overwrites(guild, 100)
        assert len(result) == 1  # owner only
