"""Lobby configuration helpers.

ロビーごとの作成ルール・操作権限・機能フラグを解釈する純粋関数群。
DB モデルのカラム名を UI / Cog 側に散らさないための薄いレイヤー。
"""

from __future__ import annotations

import re
from typing import Any, Final

LOBBY_NAMING_PERSONAL: Final = "personal"
LOBBY_NAMING_NUMBERED: Final = "numbered"

LOBBY_OWNER_MODE_OWNER: Final = "owner"
LOBBY_OWNER_MODE_NONE: Final = "none"

LOBBY_CONTROL_OWNER: Final = "owner"
LOBBY_CONTROL_MEMBERS: Final = "members"
LOBBY_CONTROL_ADMINS: Final = "admins"

NUMBER_STYLE_HALF: Final = "half"
NUMBER_STYLE_FULL: Final = "full"
NUMBER_MATCH_HALF: Final = "half"
NUMBER_MATCH_FULL: Final = "full"
NUMBER_MATCH_BOTH: Final = "both"

FEATURE_RENAME: Final = "rename"
FEATURE_LIMIT: Final = "limit"
FEATURE_BITRATE: Final = "bitrate"
FEATURE_REGION: Final = "region"
FEATURE_LOCK: Final = "lock"
FEATURE_HIDE: Final = "hide"
FEATURE_NSFW: Final = "nsfw"
FEATURE_TRANSFER: Final = "transfer"
FEATURE_KICK: Final = "kick"
FEATURE_DISSOLVE: Final = "dissolve"
FEATURE_BLOCK: Final = "block"
FEATURE_ALLOW: Final = "allow"
FEATURE_CAMERA: Final = "camera"

ALL_FEATURES: Final = (
    FEATURE_RENAME,
    FEATURE_LIMIT,
    FEATURE_BITRATE,
    FEATURE_REGION,
    FEATURE_LOCK,
    FEATURE_HIDE,
    FEATURE_NSFW,
    FEATURE_TRANSFER,
    FEATURE_KICK,
    FEATURE_DISSOLVE,
    FEATURE_BLOCK,
    FEATURE_ALLOW,
    FEATURE_CAMERA,
)

FEATURE_FIELD_BY_NAME: Final = {
    FEATURE_RENAME: "allow_rename",
    FEATURE_LIMIT: "allow_limit",
    FEATURE_BITRATE: "allow_bitrate",
    FEATURE_REGION: "allow_region",
    FEATURE_LOCK: "allow_lock",
    FEATURE_HIDE: "allow_hide",
    FEATURE_NSFW: "allow_nsfw",
    FEATURE_TRANSFER: "allow_transfer",
    FEATURE_KICK: "allow_kick",
    FEATURE_DISSOLVE: "allow_dissolve",
    FEATURE_BLOCK: "allow_block",
    FEATURE_ALLOW: "allow_allow",
    FEATURE_CAMERA: "allow_camera",
}

FEATURE_PRESET_FULL: Final = "full"
FEATURE_PRESET_LIMIT_ONLY: Final = "limit_only"

_FULL_WIDTH_DIGITS = "０１２３４５６７８９"
_HALF_WIDTH_DIGITS = "0123456789"
_HALF_TO_FULL = str.maketrans(_HALF_WIDTH_DIGITS, _FULL_WIDTH_DIGITS)
_FULL_TO_HALF = str.maketrans(_FULL_WIDTH_DIGITS, _HALF_WIDTH_DIGITS)


def is_numbered_lobby(lobby: Any) -> bool:
    """ロビーが連番命名モードかどうかを返す。"""
    return getattr(lobby, "naming_mode", LOBBY_NAMING_PERSONAL) == LOBBY_NAMING_NUMBERED


def has_owner(lobby: Any) -> bool:
    """ロビーから作られる VC にオーナー概念があるかを返す。"""
    owner_mode = getattr(lobby, "owner_mode", LOBBY_OWNER_MODE_OWNER)
    if owner_mode in {LOBBY_OWNER_MODE_OWNER, LOBBY_OWNER_MODE_NONE}:
        return owner_mode == LOBBY_OWNER_MODE_OWNER
    return True


def get_control_policy(lobby: Any) -> str:
    """ロビーの操作権限ポリシーを返す。"""
    policy = getattr(lobby, "control_policy", LOBBY_CONTROL_OWNER)
    if policy in {LOBBY_CONTROL_OWNER, LOBBY_CONTROL_MEMBERS, LOBBY_CONTROL_ADMINS}:
        return str(policy)
    return LOBBY_CONTROL_OWNER


def is_feature_enabled(lobby: Any | None, feature: str) -> bool:
    """ロビーで指定機能が有効かどうかを返す。

    lobby が None の場合は後方互換として全機能有効にする。
    """
    if lobby is None:
        return True
    field = FEATURE_FIELD_BY_NAME.get(feature)
    if field is None:
        return False
    return bool(getattr(lobby, field, True))


def enabled_features(lobby: Any | None) -> set[str]:
    """ロビーで有効な機能名セットを返す。"""
    return {feature for feature in ALL_FEATURES if is_feature_enabled(lobby, feature)}


def feature_flags_for_preset(preset: str) -> dict[str, bool]:
    """機能プリセットから DB カラム向けの bool 辞書を作る。"""
    if preset == FEATURE_PRESET_LIMIT_ONLY:
        return {
            field: feature == FEATURE_LIMIT
            for feature, field in FEATURE_FIELD_BY_NAME.items()
        }
    return dict.fromkeys(FEATURE_FIELD_BY_NAME.values(), True)


def format_sequence_number(number: int, style: str) -> str:
    """連番を半角/全角の設定に合わせて文字列化する。"""
    raw = str(number)
    if style == NUMBER_STYLE_FULL:
        return raw.translate(_HALF_TO_FULL)
    return raw


def parse_sequence_number(name: str, prefix: str, match_mode: str) -> int | None:
    """チャンネル名から prefix に続く連番を抽出する。

    完全一致のみを対象にする。例: prefix=作業空間 の場合、
    作業空間1 / 作業空間１ は読むが、作業空間1-backup は読まない。
    """
    if not prefix:
        return None

    if match_mode == NUMBER_MATCH_HALF:
        pattern = rf"^{re.escape(prefix)}([0-9]+)$"
    elif match_mode == NUMBER_MATCH_FULL:
        pattern = rf"^{re.escape(prefix)}([{_FULL_WIDTH_DIGITS}]+)$"
    else:
        pattern = rf"^{re.escape(prefix)}([0-9{_FULL_WIDTH_DIGITS}]+)$"

    match = re.fullmatch(pattern, name)
    if not match:
        return None

    return int(match.group(1).translate(_FULL_TO_HALF))
