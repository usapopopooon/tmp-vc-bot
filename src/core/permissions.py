"""Pure functions for permission calculations.

Discord のチャンネル権限 (PermissionOverwrite) を組み立てる純粋関数群。
副作用 (DB・API 呼び出し) を持たないので、テストしやすい。

Discord の権限モデル:
  - @everyone (default_role) にデフォルト権限を設定
  - 個別メンバーに上書き (overwrite) を設定
  - overwrite の値: True=許可, False=拒否, None=ロールの設定に従う
"""

import discord


def build_locked_overwrites(
    guild: discord.Guild,
    owner_id: int,
    allowed_user_ids: list[int] | None = None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """ロック状態のチャンネル権限を構築する。

    ロック時の挙動:
      - @everyone: connect=False (全員接続不可)
      - オーナー: 全権限 (接続・発言・配信・移動・ミュート・スピーカーオフ)
      - 許可リスト: connect=True (接続のみ許可)

    Args:
        guild: Discord サーバーオブジェクト
        owner_id: チャンネルオーナーの Discord ユーザー ID
        allowed_user_ids: 接続を許可するユーザー ID のリスト (オプション)

    Returns:
        {メンバー/ロール: 権限設定} の辞書。
        channel.edit(overwrites=...) に渡す。
    """
    # Snowflake = Discord の ID を持つオブジェクト (Member, Role 等)
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        # @everyone (サーバー全員) の接続を拒否
        guild.default_role: discord.PermissionOverwrite(connect=False),
    }

    # オーナーにはフルアクセスを付与
    owner = guild.get_member(owner_id)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(
            connect=True,  # VC に接続できる
            speak=True,  # 発言できる
            stream=True,  # 画面共有できる
            move_members=True,  # 他メンバーを移動できる
            mute_members=True,  # 他メンバーをミュートできる
            deafen_members=True,  # 他メンバーのスピーカーをオフにできる
        )

    # 許可リストのユーザーには接続のみ許可
    if allowed_user_ids:
        for user_id in allowed_user_ids:
            member = guild.get_member(user_id)
            if member:
                overwrites[member] = discord.PermissionOverwrite(connect=True)

    return overwrites


def build_unlocked_overwrites(
    guild: discord.Guild,
    owner_id: int,
    blocked_user_ids: list[int] | None = None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """ロック解除状態のチャンネル権限を構築する。

    ロック解除時の挙動:
      - @everyone: デフォルト権限 (overwrite なし)
      - オーナー: モデレーション権限 (移動・ミュート等)
      - ブロックリスト: connect=False (接続拒否)

    Args:
        guild: Discord サーバーオブジェクト
        owner_id: チャンネルオーナーの Discord ユーザー ID
        blocked_user_ids: 接続を拒否するユーザー ID のリスト (オプション)

    Returns:
        {メンバー/ロール: 権限設定} の辞書
    """
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

    # オーナーにモデレーション権限を付与
    owner = guild.get_member(owner_id)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(
            connect=True,
            speak=True,
            stream=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )

    # ブロックされたユーザーの接続を拒否
    if blocked_user_ids:
        for user_id in blocked_user_ids:
            member = guild.get_member(user_id)
            if member:
                overwrites[member] = discord.PermissionOverwrite(connect=False)

    return overwrites


def is_owner(session_owner_id: str, user_id: int) -> bool:
    """ユーザーがチャンネルオーナーかどうかを判定する。

    DB には owner_id を文字列 (str) で保存しているが、
    Discord API は int で返すため、型を合わせて比較する。

    Args:
        session_owner_id: DB に保存されたオーナー ID (文字列)
        user_id: 判定したいユーザーの ID (整数)

    Returns:
        オーナーなら True
    """
    return session_owner_id == str(user_id)
