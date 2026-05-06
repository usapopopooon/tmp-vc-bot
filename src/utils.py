"""Shared utility functions."""

from __future__ import annotations

import asyncio
import time

# =============================================================================
# リソースロック管理 (並行処理の競合防止)
# =============================================================================

# リソースごとのロックを管理
# key: resource_key (任意の文字列), value: (asyncio.Lock, last_access_time)
_resource_locks: dict[str, tuple[asyncio.Lock, float]] = {}

# ロッククリーンアップ間隔
_LOCK_CLEANUP_INTERVAL = 600  # 10分
_lock_last_cleanup_time = float("-inf")

# 未使用ロックの保持時間
_LOCK_EXPIRY_TIME = 300  # 5分


def _cleanup_resource_locks() -> None:
    """古い未使用ロックを削除する."""
    global _lock_last_cleanup_time
    now = time.monotonic()

    # 10分ごとにクリーンアップ
    if (
        _lock_last_cleanup_time > 0
        and now - _lock_last_cleanup_time < _LOCK_CLEANUP_INTERVAL
    ):
        return

    _lock_last_cleanup_time = now

    # 1パス削除: キーのスナップショットから期限切れをその場で削除
    for key in list(_resource_locks):
        lock, last_access = _resource_locks[key]
        if now - last_access > _LOCK_EXPIRY_TIME and not lock.locked():
            del _resource_locks[key]


def get_resource_lock(resource_key: str) -> asyncio.Lock:
    """リソースキーに対応するロックを取得する.

    同じリソースキーに対しては常に同じロックインスタンスを返す。
    これにより、同一リソースへの同時アクセスを防止できる。

    Args:
        resource_key: リソースを識別するキー
            例: "channel:123456", "vc_create:user:789"

    Returns:
        asyncio.Lock インスタンス
    """
    _cleanup_resource_locks()

    now = time.monotonic()

    entry = _resource_locks.get(resource_key)
    if entry is None:
        lock = asyncio.Lock()
        _resource_locks[resource_key] = (lock, now)
        return lock

    _resource_locks[resource_key] = (entry[0], now)
    return entry[0]


def clear_resource_locks() -> None:
    """全てのリソースロックをクリアする (テスト用)."""
    global _lock_last_cleanup_time
    _resource_locks.clear()
    _lock_last_cleanup_time = float("-inf")


def get_resource_lock_count() -> int:
    """現在管理されているロックの数を返す (テスト/デバッグ用)."""
    return len(_resource_locks)
