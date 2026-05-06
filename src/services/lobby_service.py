"""Lobby, VoiceSession, VoiceSessionMember の DB 操作。"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Lobby, VoiceSession, VoiceSessionMember

__all__ = [
    "add_voice_session_member",
    "create_lobby",
    "create_voice_session",
    "delete_lobby",
    "delete_lobbies_by_guild",
    "delete_voice_session",
    "delete_voice_sessions_by_guild",
    "get_all_lobbies",
    "get_all_voice_sessions",
    "get_lobbies_by_guild",
    "get_lobby_by_channel_id",
    "get_voice_session",
    "get_voice_session_members_ordered",
    "remove_voice_session_member",
    "update_voice_session",
]


# =============================================================================
# Lobby (ロビー) 操作
# =============================================================================


async def get_lobby_by_channel_id(
    session: AsyncSession, channel_id: str
) -> Lobby | None:
    """チャンネル ID からロビーを取得する。

    ユーザーが VC に参加したとき、そのチャンネルがロビーかどうかを
    判定するために使う。

    Args:
        session (AsyncSession): DB セッション。
        channel_id (str): 調べたい Discord チャンネルの ID (文字列)。

    Returns:
        Lobby | None: ロビーが見つかれば Lobby オブジェクト、なければ None。

    Raises:
        sqlalchemy.exc.MultipleResultsFound: 同じ channel_id のレコードが
            複数存在する場合 (通常は発生しない、ユニーク制約あり)。

    Examples:
        ロビー判定::

            async with async_session() as session:
                lobby = await get_lobby_by_channel_id(session, channel_id)
                if lobby:
                    # ロビーに参加した → 一時 VC を作成
                    pass
                else:
                    # 通常の VC に参加した
                    pass

    See Also:
        - :func:`create_lobby`: ロビー作成
        - :class:`src.database.models.Lobby`: ロビーモデル
    """
    # select(Lobby) → SELECT * FROM lobbies
    # .where(...) → WHERE lobby_channel_id = :channel_id
    result = await session.execute(
        select(Lobby).where(Lobby.lobby_channel_id == channel_id)
    )
    # scalar_one_or_none: 結果が1行なら返す、0行なら None、2行以上ならエラー
    return result.scalar_one_or_none()


async def get_lobbies_by_guild(session: AsyncSession, guild_id: str) -> list[Lobby]:
    """サーバー (guild) に属する全ロビーを取得する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        ロビーのリスト (0件なら空リスト)
    """
    result = await session.execute(select(Lobby).where(Lobby.guild_id == guild_id))
    # scalars().all() で全行を取得し、list() でリストに変換
    return list(result.scalars().all())


async def create_lobby(
    session: AsyncSession,
    guild_id: str,
    lobby_channel_id: str,
    category_id: str | None = None,
    default_user_limit: int = 0,
) -> Lobby:
    """新しいロビーを DB に登録する。

    /lobby コマンドで VC を作成した後に呼ばれる。

    Args:
        session (AsyncSession): DB セッション。
        guild_id (str): Discord サーバーの ID。
        lobby_channel_id (str): ロビーとして登録する VC の ID。
        category_id (str | None): 一時 VC を配置するカテゴリの ID。
            None なら同カテゴリ。
        default_user_limit (int): デフォルトの人数制限 (0 = 無制限)。

    Returns:
        Lobby: 作成された Lobby オブジェクト (id が自動採番される)。

    Raises:
        sqlalchemy.exc.IntegrityError: 同じ lobby_channel_id が既に存在する場合。

    Notes:
        - commit() を内部で呼び出す
        - refresh() で自動採番された id を取得

    Examples:
        ロビー作成::

            async with async_session() as session:
                lobby = await create_lobby(
                    session,
                    guild_id="123456789",
                    lobby_channel_id="987654321",
                    default_user_limit=10,
                )
                print(f"Created lobby with ID: {lobby.id}")

    See Also:
        - :func:`delete_lobby`: ロビー削除
        - :func:`get_lobby_by_channel_id`: ロビー取得
    """
    lobby = Lobby(
        guild_id=guild_id,
        lobby_channel_id=lobby_channel_id,
        category_id=category_id,
        default_user_limit=default_user_limit,
    )
    # session.add(): セッションにオブジェクトを追加 (INSERT 予約)
    session.add(lobby)
    # commit(): 実際に DB に書き込む (INSERT 実行)
    await session.commit()
    # refresh(): DB から最新の値を読み直す (自動採番された id を取得)
    await session.refresh(lobby)
    return lobby


async def delete_lobby(session: AsyncSession, lobby_id: int) -> bool:
    """ロビーを削除する。

    Args:
        session: DB セッション
        lobby_id: 削除するロビーの ID

    Returns:
        削除できたら True、見つからなければ False
    """
    result = await session.execute(select(Lobby).where(Lobby.id == lobby_id))
    lobby = result.scalar_one_or_none()
    if lobby:
        # session.delete() + commit() で DELETE 文を実行
        await session.delete(lobby)
        await session.commit()
        return True
    return False


async def delete_lobbies_by_guild(session: AsyncSession, guild_id: str) -> int:
    """指定ギルドの全ロビーを削除する。

    Bot がギルドから退出したときにクリーンアップとして使用。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        削除したロビーの数
    """
    result = await session.execute(delete(Lobby).where(Lobby.guild_id == guild_id))
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


async def get_all_lobbies(session: AsyncSession) -> list[Lobby]:
    """全てのロビーを取得する。

    Args:
        session: DB セッション

    Returns:
        全ロビーのリスト
    """
    result = await session.execute(select(Lobby))
    return list(result.scalars().all())


# =============================================================================
# VoiceSession (一時 VC セッション) 操作
# =============================================================================


async def get_voice_session(
    session: AsyncSession, channel_id: str
) -> VoiceSession | None:
    """チャンネル ID から VC セッションを取得する。

    チャンネルが一時 VC かどうかの判定や、オーナー情報の取得に使う。

    Args:
        session: DB セッション
        channel_id: Discord VC のチャンネル ID

    Returns:
        セッションが見つかれば VoiceSession、なければ None
    """
    result = await session.execute(
        select(VoiceSession).where(VoiceSession.channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def get_all_voice_sessions(session: AsyncSession) -> list[VoiceSession]:
    """全てのアクティブな VC セッションを取得する。

    Bot 起動時に永続 View を復元するために使う。

    Args:
        session: DB セッション

    Returns:
        全 VoiceSession のリスト
    """
    result = await session.execute(select(VoiceSession))
    return list(result.scalars().all())


async def create_voice_session(
    session: AsyncSession,
    lobby_id: int,
    channel_id: str,
    owner_id: str,
    name: str,
    user_limit: int = 0,
) -> VoiceSession:
    """新しい VC セッションを DB に登録する。

    ユーザーがロビーに参加し、一時 VC が作成された直後に呼ばれる。

    Args:
        session (AsyncSession): DB セッション。
        lobby_id (int): 親ロビーの ID。
        channel_id (str): 作成された一時 VC の ID。
        owner_id (str): チャンネルオーナーの Discord ユーザー ID。
        name (str): チャンネル名。
        user_limit (int): 人数制限 (0 = 無制限)。

    Returns:
        VoiceSession: 作成された VoiceSession オブジェクト。

    Raises:
        sqlalchemy.exc.IntegrityError: 同じ channel_id が既に存在する場合、
            または存在しない lobby_id を指定した場合。

    Notes:
        - commit() を内部で呼び出す
        - 作成後、オーナーを VoiceSessionMember として追加する必要がある

    Examples:
        セッション作成::

            async with async_session() as session:
                voice_session = await create_voice_session(
                    session,
                    lobby_id=lobby.id,
                    channel_id=str(channel.id),
                    owner_id=str(member.id),
                    name=f"{member.display_name}'s channel",
                )
                # オーナーをメンバーとして追加
                await add_voice_session_member(
                    session, voice_session.id, str(member.id)
                )

    See Also:
        - :func:`delete_voice_session`: セッション削除
        - :func:`update_voice_session`: セッション更新
        - :func:`add_voice_session_member`: メンバー追加
    """
    voice_session = VoiceSession(
        lobby_id=lobby_id,
        channel_id=channel_id,
        owner_id=owner_id,
        name=name,
        user_limit=user_limit,
    )
    session.add(voice_session)
    await session.commit()
    await session.refresh(voice_session)
    return voice_session


async def update_voice_session(
    session: AsyncSession,
    voice_session: VoiceSession,
    *,
    name: str | None = None,
    user_limit: int | None = None,
    is_locked: bool | None = None,
    is_hidden: bool | None = None,
    owner_id: str | None = None,
) -> VoiceSession:
    """VC セッションの情報を更新する。

    コントロールパネルのボタン操作 (名前変更、ロック、非表示、オーナー譲渡)
    で呼ばれる。None のフィールドは変更しない。

    Args:
        session (AsyncSession): DB セッション。
        voice_session (VoiceSession): 更新対象の VoiceSession オブジェクト。
        name (str | None): 新しいチャンネル名 (None なら変更しない)。
        user_limit (int | None): 新しい人数制限 (None なら変更しない)。
        is_locked (bool | None): 新しいロック状態 (None なら変更しない)。
        is_hidden (bool | None): 新しい非表示状態 (None なら変更しない)。
        owner_id (str | None): 新しいオーナー ID (None なら変更しない)。

    Returns:
        VoiceSession: 更新後の VoiceSession オブジェクト。

    Notes:
        - キーワード引数のみ受け付ける (``*`` による区切り)
        - 部分更新パターン: None のフィールドは変更しない
        - commit() を内部で呼び出す

    Examples:
        ロック状態のみ変更::

            async with async_session() as session:
                voice_session = await get_voice_session(session, channel_id)
                voice_session = await update_voice_session(
                    session,
                    voice_session,
                    is_locked=True,
                )

        複数フィールドを同時に変更::

            voice_session = await update_voice_session(
                session,
                voice_session,
                name="New Name",
                user_limit=5,
                is_hidden=True,
            )

    See Also:
        - :func:`get_voice_session`: セッション取得
        - :class:`src.database.models.VoiceSession`: セッションモデル
    """
    # None でないフィールドだけ更新する (部分更新パターン)
    if name is not None:
        voice_session.name = name
    if user_limit is not None:
        voice_session.user_limit = user_limit
    if is_locked is not None:
        voice_session.is_locked = is_locked
    if is_hidden is not None:
        voice_session.is_hidden = is_hidden
    if owner_id is not None:
        voice_session.owner_id = owner_id

    # SQLAlchemy はオブジェクトの変更を自動検知するので、
    # commit() だけで UPDATE 文が実行される
    await session.commit()
    return voice_session


async def delete_voice_session(session: AsyncSession, channel_id: str) -> bool:
    """VC セッションを削除する。

    一時 VC から全員が退出したとき、チャンネル削除と一緒に呼ばれる。

    Args:
        session: DB セッション
        channel_id: 削除する VC のチャンネル ID

    Returns:
        削除できたら True、見つからなければ False
    """
    result = await session.execute(
        select(VoiceSession).where(VoiceSession.channel_id == channel_id)
    )
    voice_session = result.scalar_one_or_none()
    if voice_session:
        await session.delete(voice_session)
        await session.commit()
        return True
    return False


async def delete_voice_sessions_by_guild(session: AsyncSession, guild_id: str) -> int:
    """指定ギルドの全 VC セッションを削除する。

    Bot がギルドから退出したときにクリーンアップとして使用。
    Lobby テーブルを経由して該当ギルドのセッションを検索。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        削除したセッションの数
    """
    # Lobby 経由で該当ギルドの VoiceSession を削除 (サブクエリ使用)
    result = await session.execute(
        delete(VoiceSession).where(
            VoiceSession.lobby_id.in_(
                select(Lobby.id).where(Lobby.guild_id == guild_id)
            )
        )
    )
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


# =============================================================================
# VoiceSessionMember (VC メンバー) 操作
# =============================================================================


async def add_voice_session_member(
    session: AsyncSession,
    voice_session_id: int,
    user_id: str,
) -> VoiceSessionMember:
    """VC セッションにメンバーを追加する。

    ユーザーが一時 VC に参加したときに呼ばれる。
    既に存在する場合は既存のレコードを返す (参加時刻は更新しない)。

    Args:
        session: DB セッション
        voice_session_id: VC セッションの ID
        user_id: メンバーの Discord ユーザー ID

    Returns:
        作成または既存の VoiceSessionMember オブジェクト
    """
    # 既存のレコードがあるか確認
    result = await session.execute(
        select(VoiceSessionMember).where(
            VoiceSessionMember.voice_session_id == voice_session_id,
            VoiceSessionMember.user_id == user_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    # 新規作成
    member = VoiceSessionMember(
        voice_session_id=voice_session_id,
        user_id=user_id,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def remove_voice_session_member(
    session: AsyncSession,
    voice_session_id: int,
    user_id: str,
) -> bool:
    """VC セッションからメンバーを削除する。

    ユーザーが一時 VC から退出したときに呼ばれる。

    Args:
        session: DB セッション
        voice_session_id: VC セッションの ID
        user_id: メンバーの Discord ユーザー ID

    Returns:
        削除できたら True、見つからなければ False
    """
    result = await session.execute(
        select(VoiceSessionMember).where(
            VoiceSessionMember.voice_session_id == voice_session_id,
            VoiceSessionMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member:
        await session.delete(member)
        await session.commit()
        return True
    return False


async def get_voice_session_members_ordered(
    session: AsyncSession,
    voice_session_id: int,
) -> list[VoiceSessionMember]:
    """VC セッションのメンバーを参加順 (古い順) で取得する。

    オーナー引き継ぎ時の優先順位を決定するために使う。

    Args:
        session: DB セッション
        voice_session_id: VC セッションの ID

    Returns:
        参加時刻が古い順にソートされた VoiceSessionMember のリスト
    """
    result = await session.execute(
        select(VoiceSessionMember)
        .where(VoiceSessionMember.voice_session_id == voice_session_id)
        .order_by(VoiceSessionMember.joined_at, VoiceSessionMember.user_id)
    )
    return list(result.scalars().all())
