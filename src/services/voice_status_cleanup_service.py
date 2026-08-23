"""カテゴリ別の空室時ボイスチャンネルステータス除去設定。"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import VoiceStatusCleanupConfig

__all__ = [
    "delete_voice_status_cleanup_by_guild",
    "delete_voice_status_cleanup_config",
    "get_voice_status_cleanup_config",
    "list_voice_status_cleanup_configs",
    "set_voice_status_cleanup_config",
]


def _rowcount(result: Any) -> int:
    """SQLAlchemy の rowcount を int として扱う。"""
    return int(getattr(result, "rowcount", 0) or 0)


async def get_voice_status_cleanup_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
) -> VoiceStatusCleanupConfig | None:
    """指定カテゴリのステータス除去設定を取得する。"""
    result = await session.execute(
        select(VoiceStatusCleanupConfig).where(
            VoiceStatusCleanupConfig.guild_id == guild_id,
            VoiceStatusCleanupConfig.category_id == category_id,
        )
    )
    return result.scalar_one_or_none()


async def list_voice_status_cleanup_configs(
    session: AsyncSession,
    guild_id: str,
) -> list[VoiceStatusCleanupConfig]:
    """サーバー内のステータス除去設定を作成順に取得する。"""
    result = await session.execute(
        select(VoiceStatusCleanupConfig)
        .where(VoiceStatusCleanupConfig.guild_id == guild_id)
        .order_by(
            VoiceStatusCleanupConfig.created_at,
            VoiceStatusCleanupConfig.id,
        )
    )
    return list(result.scalars().all())


async def set_voice_status_cleanup_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
    delay_seconds: int,
) -> VoiceStatusCleanupConfig:
    """カテゴリ設定を作成または更新する。"""
    config = await get_voice_status_cleanup_config(session, guild_id, category_id)
    if config is None:
        config = VoiceStatusCleanupConfig(
            guild_id=guild_id,
            category_id=category_id,
            delay_seconds=delay_seconds,
        )
        session.add(config)
    else:
        config.delay_seconds = delay_seconds

    await session.commit()
    await session.refresh(config)
    return config


async def delete_voice_status_cleanup_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
) -> bool:
    """指定カテゴリのステータス除去設定を削除する。"""
    result = await session.execute(
        delete(VoiceStatusCleanupConfig).where(
            VoiceStatusCleanupConfig.guild_id == guild_id,
            VoiceStatusCleanupConfig.category_id == category_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def delete_voice_status_cleanup_by_guild(
    session: AsyncSession,
    guild_id: str,
) -> int:
    """指定サーバーのステータス除去設定を全て削除する。"""
    result = await session.execute(
        delete(VoiceStatusCleanupConfig).where(
            VoiceStatusCleanupConfig.guild_id == guild_id,
        )
    )
    await session.commit()
    return _rowcount(result)
