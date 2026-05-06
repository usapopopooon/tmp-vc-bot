"""Pure validation functions.

Discord の制限値に基づいてユーザー入力を検証する純粋関数群。
副作用を持たないのでテストしやすい。
"""

# --- Discord チャンネル名の長さ制限 ---
# Discord API の仕様: チャンネル名は 1〜100 文字
MIN_CHANNEL_NAME_LENGTH = 1
MAX_CHANNEL_NAME_LENGTH = 100

# --- Discord VC の人数制限 ---
# Discord API の仕様: user_limit は 0〜99 (0 = 無制限)
MIN_USER_LIMIT = 0
MAX_USER_LIMIT = 99


def validate_user_limit(limit: int) -> bool:
    """VC の人数制限が有効な範囲かどうかを検証する。

    Args:
        limit: 検証する人数制限 (0 = 無制限、1〜99 = 制限あり)

    Returns:
        0〜99 の範囲内なら True
    """
    return MIN_USER_LIMIT <= limit <= MAX_USER_LIMIT


def validate_channel_name(name: str) -> bool:
    """チャンネル名が有効な長さかどうかを検証する。

    Args:
        name: 検証するチャンネル名

    Returns:
        1〜100 文字の範囲内なら True
    """
    return MIN_CHANNEL_NAME_LENGTH <= len(name) <= MAX_CHANNEL_NAME_LENGTH


def validate_bitrate(bitrate: int) -> bool:
    """VC のビットレートが有効な範囲かどうかを検証する。

    実際の上限はサーバーのブーストレベルによって異なる:
      - レベル0: 96 kbps
      - レベル1: 128 kbps
      - レベル2: 256 kbps
      - レベル3: 384 kbps
    ここでは最大の 384 kbps を上限として検証する。

    Args:
        bitrate: 検証するビットレート (kbps 単位)

    Returns:
        8〜384 の範囲内なら True
    """
    return 8 <= bitrate <= 384
