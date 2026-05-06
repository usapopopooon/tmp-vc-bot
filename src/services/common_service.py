"""ProcessedEvent (マルチインスタンス重複排除) の DB 操作。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import ProcessedEvent

__all__ = [
    "claim_event",
    "cleanup_expired_events",
]


async def claim_event(session: AsyncSession, event_key: str) -> bool:
    """イベントをアトミックに claim する。

    UNIQUE 制約 (event_key) を利用し、INSERT の IntegrityError で
    「既に別インスタンスが処理済み」をアトミックに判定する。

    Args:
        session: DB セッション。
        event_key: イベントを一意に識別するキー。

    Returns:
        True: このインスタンスが claim に成功 (処理を続行すべき)。
        False: 別インスタンスが既に claim 済み (処理をスキップすべき)。
    """
    try:
        session.add(ProcessedEvent(event_key=event_key))
        await session.flush()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def cleanup_expired_events(
    session: AsyncSession, max_age_seconds: int = 3600
) -> int:
    """期限切れの重複排除レコードを削除する。

    Args:
        session: DB セッション。
        max_age_seconds: レコードの最大保持期間 (秒)。デフォルト 3600 (1時間)。

    Returns:
        削除されたレコード数。
    """
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=max_age_seconds)
    stmt = delete(ProcessedEvent).where(ProcessedEvent.created_at < cutoff)
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]
