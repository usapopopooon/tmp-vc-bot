# tmp-vc-bot

[![CI](https://github.com/usapopopooon/tmp-vc-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/usapopopooon/tmp-vc-bot/actions/workflows/ci.yml)

Discord の **一時ボイスチャンネル (Ephemeral VC) 機能だけ** を提供する単機能 bot。
ロビー VC に参加すると専用 VC が自動作成され、全員が抜けると自動削除される。
ロビー設定に応じて Embed + ボタンのコントロールパネル (名前変更 / 人数制限 /
ロック / 非表示 / 譲渡 / キック / ブロック / 解散 …) が提供される。

## 機能

- `/vc lobby` — ロビー VC を作成 (管理者専用)。引数なしは従来の個人 VC ロビー、
  設定付きでは連番名・オーナーなし・機能制限付きロビーも作成可能
- `/vc lobby dialog:true` — 連番共有ロビーをダイアログ入力で作成
- ロビー VC に参加すると一時 VC を自動作成し、参加者を移動
- 全員退出で一時 VC を自動削除、オーナー退出時は最古参メンバーに自動譲渡
- コントロールパネル: 名前変更 / 人数制限 / ビットレート / リージョン / ロック /
  非表示 / 年齢制限 / 譲渡 / キック / 解散 / ブロック / 許可 / カメラ権限
- 連番ロビー: `作業空間1` / `作業空間１` のような半角・全角番号を認識し、
  設定した数字形式で空き番号の VC を作成
  - 例: `/vc lobby dialog:true` で、ロビー名 `⌛️もくもく空間作成`、
    作成VC名の前半 `⌛️もくもく空間`、開始番号 `2`、数字形式 `全角`、
    変更可能な機能 `人数のみ` を入力
- `/voice-notify` — VC 入退室通知をサーバー内の指定テキストチャンネルへ送信
- `/voice-notify-cross` — 通常通知とは別設定で、共有 ON のサーバーの VC 入退室を
  受信先設定済みの他サーバーへ通知。サーバー名リンクには管理者が設定した固定招待 URL を使う。
  通常通知の除外 VC はクロス通知でも送らず、クロス通知だけの除外 VC は
  `/voice-notify-cross exclude-add` / `exclude-remove` で別管理
- `/voice-status-cleanup` — カテゴリごとに、VC が 0 人になってから指定時間後に
  ボイスチャンネルステータスを自動除去。待機中に再入室した場合は除去を中止
- マルチインスタンス対応 (`processed_events` テーブルでアトミック重複排除)
- Bot 再起動後もコントロールパネルのボタンが動作 (永続 View)

## 必要なもの

- Python 3.12
- PostgreSQL 17 (asyncpg)
- Discord Bot トークン (Developer Portal)。複数 Bot アカウントで動かす場合は複数のトークン

## セットアップ

```bash
cp .env.example .env
# .env を編集して DISCORD_TOKEN を設定
# 複数 Bot を 1 プロセスで動かす場合は DISCORD_TOKENS=token1,token2 も利用可
# make run でホストから直接起動する場合だけ DATABASE_URL も localhost 向けに設定
make setup
make ci          # lint + type check
# Docker/Coolify と同じ構成で起動する場合
docker compose up -d
```

## 開発

```bash
make test-db-start                    # テスト用 PostgreSQL を起動
make test                             # 全テスト実行
make lint                             # ruff check + format
make typecheck                        # mypy
make ci                               # CI と同じチェック一式
make test-db-stop                     # テスト用 PostgreSQL を停止
```

## マルチサーバー運用

この bot は **複数の Discord サーバー (ギルド) で同時運用可能** に設計されています:

- DB の `lobbies` / `voice_sessions` テーブルは `guild_id` をキーに持つマルチテナント構造
- 引数なしの通常 `/vc lobby` は後方互換として 1 サーバー 1 件。
  設定付きロビーは通常ロビーと併存可能
- 各サーバーごとに独立した一時 VC が作成・管理される

bot 自体は 1 プロセスで全サーバーを捌くので、**インストール先のサーバーを増やすだけ** で
追加運用ができます (再デプロイ不要)。

### 空室 VC のステータス自動除去

管理者がカテゴリごとに設定する。待ち時間は 1〜1440 分で、省略時は 1 分。

```text
/voice-status-cleanup add category:<カテゴリ> delay_minutes:1
/voice-status-cleanup remove category:<カテゴリ>
/voice-status-cleanup status
```

対象カテゴリ内の通常 VC から人間が 0 人になると待機を開始し、待機中に人間が
入室した場合はキャンセルする。Bot は人数に含めず、Bot だけ残っている VC も除去対象。
この Bot 自身が接続していない VC も除去対象となるため、Discord の仕様上
`Set Voice Channel Status` と `Manage Channels` の両方の権限が必要。

### 複数 Bot アカウント運用

`DISCORD_TOKEN` は従来どおり単一 Bot 用として使える。複数の Bot アカウントを
1 プロセスで同時起動したい場合は、`DISCORD_TOKENS` にカンマ区切りで指定する:

```bash
DISCORD_TOKENS=token_for_bot_a,token_for_bot_b,token_for_bot_c
```

`DISCORD_TOKEN` と `DISCORD_TOKENS` は両方指定でき、重複したトークンは自動で除外される。
互換性のため、単数の `DISCORD_TOKEN` だけを設定した既存デプロイはそのまま 1 Bot として起動する。
複数 Bot 分の Gateway 接続とキャッシュを持つため、台数を増やす場合は `BOT_MEMORY_LIMIT` も余裕を持たせる。

### 招待 URL

Discord Developer Portal → OAuth2 → URL Generator で以下を選択:

- **Scopes**: `bot`, `applications.commands` (両方必須)
- **Bot Permissions**: `Manage Channels`, `Move Members`, `Connect`, `Speak`,
  `Send Messages`, `Embed Links`, `Manage Messages` (パネルのピン留めに必要),
  `Set Voice Channel Status`

生成された URL を各サーバー管理者に共有すれば、それぞれのサーバーに追加できます。

### スラッシュコマンドが表示されない場合

招待直後に `/vc lobby` が出てこないときの確認順:

1. **`SYNC_GUILD_IDS` に対象サーバーの ID をカンマ区切りで列挙して再起動** —
   グローバル同期は Discord 側で最大 1 時間かかるが、ギルド単位は即時反映。
   ```
   SYNC_GUILD_IDS=111111111111111111,222222222222222222,333333333333333333
   ```
   恒久運用ではこの変数を空にしてグローバル同期 (1 度だけ全サーバーへ伝搬) でも
   OK。新規サーバー追加のたびに即時反映したい場合だけ ID を追記する。
2. **招待 URL に `applications.commands` スコープが入っているか確認**
   (`bot` だけだとスラッシュコマンドが登録されない)。
3. **Bot 起動ログで `Synced N slash commands to guild ...` を確認** —
   0 件や同期エラーなら Cog 読み込みに失敗している。
4. **Discord クライアントを再読み込み** (Ctrl+R / Cmd+R)。
5. **`/vc lobby` は `default_permissions(administrator=True)`** のため、管理者
   以外には表示されない。サーバー設定 → 連携サービス → Bot からロール/チャンネル
   ごとに表示権限を上書き可能。

## デプロイ

### Coolify

Coolify では通常の `docker-compose.yml` を使ってデプロイする。
Discord bot は HTTP ポートを公開しないワーカーなので、ドメイン/プロキシ設定は不要。

Coolify の環境変数に最低限以下を設定する:

```bash
DISCORD_TOKEN=...
# 複数 Bot アカウントの場合:
# DISCORD_TOKENS=token_for_bot_a,token_for_bot_b
POSTGRES_PASSWORD=強いランダム文字列
```

必要に応じて以下も設定できる。未設定なら `docker-compose.yml` の
デフォルト値が使われる:

```bash
POSTGRES_USER=tmp_vc_bot
POSTGRES_DB=tmp_vc_bot
POSTGRES_HOST=db
DATABASE_URL=
DISCORD_TOKENS=
LOG_LEVEL=INFO
SYNC_GUILD_IDS=
SYNC_GUILD_ID=
DATABASE_REQUIRE_SSL=false
DB_POOL_SIZE=1
DB_MAX_OVERFLOW=1
BOT_MEMORY_LIMIT=192m
BOT_MEMORY_RESERVATION=128m
POSTGRES_MEMORY_LIMIT=160m
POSTGRES_MEMORY_RESERVATION=96m
POSTGRES_SHM_SIZE=32mb
POSTGRES_MAX_CONNECTIONS=10
POSTGRES_SHARED_BUFFERS=16MB
```

単一サーバー運用でメモリを詰めるため、Coolify 用 compose は以下に制限している:

- bot: 192MB、DB 接続プール `1 + overflow 1`
- PostgreSQL: 160MB、`max_connections=10`、`shared_buffers=16MB`

この設定で OOM が出る場合は、まず `bot.mem_limit` を `256m`、
次に `db.mem_limit` を `192m` へ上げる。

### Railway

`railway.toml` がリポジトリにあるので、Railway から GitHub リポを連携するだけで
Dockerfile を使ってデプロイされる。`DISCORD_TOKEN` と `DATABASE_URL`
(Railway の Postgres プラグイン) を環境変数に設定する。複数 Bot アカウントで動かす場合は
`DISCORD_TOKENS` にカンマ区切りで追加する。

### Heroku

`Procfile`, `runtime.txt`, `requirements.txt` を同梱。

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set DISCORD_TOKEN=...
# 複数 Bot アカウントの場合:
# heroku config:set DISCORD_TOKENS=token_for_bot_a,token_for_bot_b
git push heroku main
```

## ディレクトリ構成

```
src/
├── bot.py               # Bot クラス (EphemeralVCBot)
├── main.py              # エントリーポイント (複数 Bot 起動)
├── config.py            # pydantic-settings 設定
├── constants.py
├── utils.py             # リソースロック
├── cogs/voice.py        # 一時 VC のメインロジック (作成/削除/引き継ぎ)
├── ui/control_panel.py  # コントロールパネル (Embed + ボタン)
├── core/
│   ├── lobby_config.py  # ロビー設定・連番・機能フラグの解釈
│   ├── permissions.py   # is_owner, build_locked_overwrites など
│   └── validators.py
├── database/
│   ├── engine.py        # 非同期 SQLAlchemy エンジン
│   └── models.py        # Lobby, VoiceSession, VoiceSessionMember, ProcessedEvent
└── services/
    ├── lobby_service.py    # Lobby/VoiceSession の CRUD
    ├── common_service.py   # claim_event (重複排除)
    └── db_service.py       # 上記の re-export
alembic/versions/         # DB マイグレーション
tests/                    # pytest (約 430 件、カバレッジ 86%+)
```

## 元プロジェクト

このリポジトリは [`discord-util-bot`](../discord-util-bot)
から一時 VC 機能だけを切り出した派生版。
