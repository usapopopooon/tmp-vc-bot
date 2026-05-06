# tmp-vc-bot

[![CI](https://github.com/usapopopooon/tmp-vc-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/usapopopooon/tmp-vc-bot/actions/workflows/ci.yml)

Discord の **一時ボイスチャンネル (Ephemeral VC) 機能だけ** を提供する単機能 bot。
ロビー VC に参加すると専用 VC が自動作成され、全員が抜けると自動削除される。
オーナーには Embed + ボタンのコントロールパネル (名前変更 / 人数制限 / ロック /
非表示 / 譲渡 / キック / ブロック / 解散 …) が提供される。

## 機能

- `/vc lobby` — ロビー VC を作成 (管理者専用)
- ロビー VC に参加すると一時 VC を自動作成し、参加者を移動
- 全員退出で一時 VC を自動削除、オーナー退出時は最古参メンバーに自動譲渡
- コントロールパネル: 名前変更 / 人数制限 / ビットレート / リージョン / ロック /
  非表示 / 年齢制限 / 譲渡 / キック / 解散 / ブロック / 許可 / カメラ権限
- マルチインスタンス対応 (`processed_events` テーブルでアトミック重複排除)
- Bot 再起動後もコントロールパネルのボタンが動作 (永続 View)

## 必要なもの

- Python 3.12
- PostgreSQL 17 (asyncpg)
- Discord Bot トークン (Developer Portal)

## セットアップ

```bash
cp .env.example .env
# .env を編集して DISCORD_TOKEN と DATABASE_URL を設定
make setup
make ci          # lint + type check
docker compose up -d db
.venv/bin/alembic upgrade head
make run
```

## 開発

```bash
make test-db-start                    # テスト用 PostgreSQL を起動
make test                             # 全テスト実行
make lint                             # ruff check + format
make typecheck                        # mypy
make ci                               # CI と同じチェック一式
```

Docker 経由:

```bash
docker compose --profile dev up test  # テスト
docker compose --profile dev up lint  # lint
```

## スラッシュコマンドが表示されない場合

招待直後に `/vc lobby` が出てこないときの確認順:

1. **`SYNC_GUILD_ID` を設定** — 環境変数に対象サーバーの ID を入れて再起動。
   グローバル同期は Discord 側で最大 1 時間かかるが、ギルド単位は即時反映。
   ```
   SYNC_GUILD_ID=123456789012345678
   ```
2. **Bot 招待 URL に `applications.commands` スコープが入っているか確認** —
   `bot` だけだとスラッシュコマンドが登録されない。両方チェックして招待し直す。
3. **Bot 起動ログで `Synced N slash commands` を確認** — 0 件なら Cog 読み込みに失敗している。
4. **Discord クライアントを再読み込み** (Ctrl+R / Cmd+R) — キャッシュが残っていることがある。
5. **`/vc lobby` は `default_permissions(administrator=True)`** のため、管理者
   以外には表示されない。サーバー設定 → 連携サービス → Bot からロール/チャンネル
   ごとに表示権限を上書き可能。

## デプロイ

### Railway

`railway.toml` がリポジトリにあるので、Railway から GitHub リポを連携するだけで
Dockerfile を使ってデプロイされる。`DISCORD_TOKEN` と `DATABASE_URL`
(Railway の Postgres プラグイン) を環境変数に設定する。

### Heroku

`Procfile`, `runtime.txt`, `requirements.txt` を同梱。

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set DISCORD_TOKEN=...
git push heroku main
```

## ディレクトリ構成

```
src/
├── bot.py               # Bot クラス (EphemeralVCBot)
├── main.py              # エントリーポイント
├── config.py            # pydantic-settings 設定
├── constants.py
├── utils.py             # リソースロック
├── cogs/voice.py        # 一時 VC のメインロジック (作成/削除/引き継ぎ)
├── ui/control_panel.py  # コントロールパネル (Embed + ボタン)
├── core/
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
