"""Entry point for Ephemeral VC bot.

Bot のエントリーポイント。データベース接続の確認、シグナルハンドラの設定、
Bot の起動を行う。

Examples:
    直接実行::

        python -m src.main

See Also:
    - :class:`src.bot.EphemeralVCBot`: Bot 本体
    - :mod:`src.config`: 設定管理

Notes:
    起動前に以下を確認すること (デプロイ環境側で済ませる):

    - 環境変数 DISCORD_TOKEN が設定されている
    - データベースが起動している
    - **Alembic マイグレーションが実行済み**
      (Dockerfile CMD の `alembic upgrade head &&` 前段、
      または Procfile の `release` フェーズで実行する)

    環境変数:

    - LOG_LEVEL: ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL)。デフォルト: INFO
"""

import asyncio
import logging
import os
import signal
import sys
from types import FrameType

from src.bot import EphemeralVCBot
from src.config import settings
from src.database.engine import check_database_connection_with_retry


def _setup_logging() -> None:
    """ロギングを設定する。

    環境変数 LOG_LEVEL からログレベルを取得し、設定する。
    デフォルトは INFO。無効な値が指定された場合も INFO を使用。
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, None)

    if not isinstance(log_level, int):
        log_level = logging.INFO
        print(f"Warning: Invalid LOG_LEVEL '{log_level_name}', using INFO")

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


_setup_logging()
logger = logging.getLogger(__name__)

#: グローバル変数として Bot インスタンスを保持。シグナルハンドラから参照する。
_bot: EphemeralVCBot | None = None


def _handle_shutdown_signal(signum: int, _frame: FrameType | None) -> None:
    """シャットダウンシグナルハンドラ (SIGTERM/SIGINT)。"""
    try:
        sig_name = signal.Signals(signum).name
    except ValueError:
        sig_name = str(signum)

    logger.info("Received %s signal, initiating graceful shutdown...", sig_name)

    if _bot is not None:
        try:
            asyncio.create_task(_shutdown_bot())
        except RuntimeError:
            logger.warning("Event loop not running, forcing shutdown")
            sys.exit(0)


async def _shutdown_bot() -> None:
    """Bot を graceful に停止する。"""
    global _bot
    if _bot is not None:
        logger.info("Closing bot connection...")
        await _bot.close()
        logger.info("Bot closed successfully")


async def main() -> None:
    """Bot のメインエントリーポイント。DB 確認 → シグナルハンドラ → Bot 起動。"""
    global _bot

    # データベース接続チェック (リトライ付き)
    if not await check_database_connection_with_retry():
        logger.error(
            "Cannot start bot: Database connection failed. "
            "Check DATABASE_URL and ensure the database is running."
        )
        sys.exit(1)

    # シグナルハンドラを設定 (Unix 系 OS)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_shutdown_signal)
            logger.info("%s handler registered", sig.name)
        except (ValueError, OSError) as e:
            logger.warning("Could not register %s handler: %s", sig.name, e)

    # SIGHUP を無視する (ターミナル切断時の保護)
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            logger.info("SIGHUP handler registered (ignored)")
        except (ValueError, OSError) as e:
            logger.warning("Could not register SIGHUP handler: %s", e)

    # SIGPIPE を無視する (切断ソケット書き込みでプロセスが終了するのを防ぐ)
    if hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
            logger.info("SIGPIPE handler registered (ignored)")
        except (ValueError, OSError) as e:
            logger.warning("Could not register SIGPIPE handler: %s", e)

    _bot = EphemeralVCBot()
    async with _bot:
        await _bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
