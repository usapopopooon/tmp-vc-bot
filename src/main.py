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
    起動シーケンス:

    1. DB 接続確認 (リトライ付き)
    2. Alembic マイグレーション (alembic upgrade head)
    3. シグナルハンドラ登録
    4. Bot 起動

    環境変数:

    - DISCORD_TOKEN: Discord Bot トークン (必須)
    - DISCORD_TOKENS: 複数 Bot 用 Discord Bot トークン (カンマ区切り、任意)
    - DATABASE_URL: DB 接続 URL
    - LOG_LEVEL: ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL)。デフォルト: INFO
"""

import asyncio
import logging
import os
import signal
import sys
from types import FrameType

from alembic.config import Config

from alembic import command
from src.bot import EphemeralVCBot
from src.cogs.voice import register_cross_guild_voice_notify_bot
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
_bots: list[EphemeralVCBot] = []
_shutdown_requested = False


def _handle_shutdown_signal(signum: int, _frame: FrameType | None) -> None:
    """シャットダウンシグナルハンドラ (SIGTERM/SIGINT)。"""
    global _shutdown_requested

    try:
        sig_name = signal.Signals(signum).name
    except ValueError:
        sig_name = str(signum)

    _shutdown_requested = True
    logger.info("Received %s signal, initiating graceful shutdown...", sig_name)

    if _bots:
        try:
            asyncio.create_task(_shutdown_bots())
        except RuntimeError:
            logger.warning("Event loop not running, forcing shutdown")
            sys.exit(0)


async def _shutdown_bots() -> None:
    """Bot を graceful に停止する。"""
    if not _bots:
        return

    logger.info("Closing %d bot connection(s)...", len(_bots))
    results = await asyncio.gather(
        *(bot.close() for bot in _bots if not bot.is_closed()),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Bot close failed: %s", result)
    logger.info("Bot connection(s) closed successfully")


def _run_migrations() -> None:
    """Alembic マイグレーションを起動時に適用する。"""
    cfg = Config("alembic.ini")
    logger.info("Running alembic upgrade head")
    command.upgrade(cfg, "head")
    logger.info("alembic upgrade head completed")


async def _run_bot(token: str, bot: EphemeralVCBot) -> None:
    """1 つの Discord Bot トークンで Bot を起動する。"""
    async with bot:
        await bot.start(token)


def _log_bot_task_result(task: asyncio.Future[None]) -> None:
    """Bot タスクが異常終了したら即時ログに出す。"""
    if task.cancelled():
        return

    exc = task.exception()
    if exc is None:
        return

    name = task.get_name() if isinstance(task, asyncio.Task) else "bot task"
    logger.error(
        "%s stopped with an error",
        name,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


async def main() -> None:
    """Bot のメインエントリーポイント。DB → migration → シグナル → Bot 起動。"""
    global _bots, _shutdown_requested
    _shutdown_requested = False

    # データベース接続チェック (リトライ付き)
    if not await check_database_connection_with_retry():
        logger.error(
            "Cannot start bot: Database connection failed. "
            "Check DATABASE_URL and ensure the database is running."
        )
        sys.exit(1)

    # マイグレーション
    _run_migrations()

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

    tokens = settings.discord_tokens
    logger.info("Starting %d bot instance(s)", len(tokens))
    _bots = [EphemeralVCBot() for _ in tokens]
    for bot in _bots:
        register_cross_guild_voice_notify_bot(bot)
    tasks = [
        asyncio.create_task(_run_bot(token, bot), name=f"bot-start-{index}")
        for index, (token, bot) in enumerate(zip(tokens, _bots, strict=True), start=1)
    ]
    for task in tasks:
        task.add_done_callback(_log_bot_task_result)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures and not _shutdown_requested:
            raise RuntimeError(f"{len(failures)} bot instance(s) stopped with an error")
    finally:
        await _shutdown_bots()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
