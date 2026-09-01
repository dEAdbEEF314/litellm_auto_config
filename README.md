# LiteLLM Proxy & 動的モデル自動選定システム

OpenRouter の最新利用データ（人気度・特別割引）およびベンチマーク（ModelGrep / Artificial Analysis）を活用し、常に最適な LLM を自動選定して LiteLLM Proxy に動的連携・配信する統合 LLM ゲートウェイ環境です。

---

## 1. このシステムが実施すること

- **統一 LLM エンドポイントの提供**:
  - ローカル LLM（Ollama 等）とクラウド LLM（OpenRouter、Anthropic 等）を単一の OpenAI 互換エンドポイント (`http://localhost:4000/v1`) に集約します。
- **動的モデル選定・自動更新 (`update_config.py`)**:
  - OpenRouter のカタログと週間トークン消費量、および特別割引セール情報を解析し、用途別の 3 カテゴリ（各 3 モデル）を自動選定して `config.yaml` を生成します。
  - **`popularity`**: 総合人気モデル（特別割引優先）
  - **`practical`**: 高機能・実用モデル（知性指数 50+、推論モデル、特別割引優先）
  - **`great_deal`**: 特別割引中の格安・高コスパモデル（最安順＆人気順）
- **自動フォールバックと冗長化**:
  - 各カテゴリの 1 位モデルで障害やレート制限が発生した場合、自動的に 2 位・3 位のモデルへとフォールバックします。
- **Discord 通知**:
  - モデル更新結果（選定モデル名、価格、機能、概要）を Discord Webhook 経由でリッチな Embed 形式で通知します。

---

## 2. ディレクトリ構成

```text
.
├── docker-compose.yml       # LiteLLM Proxy / PostgreSQL / Qdrant 構成
├── update_config.py         # 動的モデル自動選定・設定更新スクリプト
├── pyproject.toml           # Python / uv 仮想環境定義
├── base_llm.yaml            # ローカルモデル・ベース設定 (Git管理外)
├── base_llm.yaml.example    # base_llm.yaml のテンプレート・ひな形
├── config.yaml              # update_config.py が自動生成する LiteLLM 設定
├── .env                     # 環境変数・APIキー (Git管理外)
├── .env.example             # .env のテンプレート・ひな形
├── plans/                   # 各種仕様書・設計書
│   └── model-selection-spec.md  # 動的モデル選定の詳細仕様書
└── README.md                # 本ドキュメント
```

---

## 3. 設定ファイルの作成方法

初回セットアップ時は、テンプレートから設定ファイルを作成します。

### ① 環境変数ファイル (`.env`) の作成

```bash
cp .env.example .env
```

`.env` を開き、以下の項目を設定します：

| 環境変数名 | 必須 | 説明・設定例 |
| :--- | :---: | :--- |
| `OPENROUTER_API_KEY` | **必須** | OpenRouter (https://openrouter.ai/) の API キー (`sk-or-v1-...`) |
| `LITELLM_MASTER_KEY` | **必須** | LiteLLM Proxy の認証トークン (`sk-...` 任意の文字列) |
| `POSTGRES_DB` | **必須** | PostgreSQL データベース名 (例: `litellm_db`) |
| `POSTGRES_USER` | **必須** | PostgreSQL ユーザー名 (例: `litellm_user`) |
| `POSTGRES_PASSWORD` | **必須** | PostgreSQL パスワード |
| `DISCORD_WEBHOOK_URL` | 任意 | 更新通知用 Discord Webhook URL |
| `MODELGREP_COMPARE_ENABLED` | 任意 | 詳細比較レポートの出力フラグ (`1` = 有効, `0` = 無効) |

### ② ベース設定ファイル (`base_llm.yaml`) の作成

```bash
cp base_llm.yaml.example base_llm.yaml
```

`base_llm.yaml` には、ローカルで稼働している Ollama モデルや固定で公開したいモデルを記述します。

```yaml
model_list:
  - model_name: local-worker-coder
    litellm_params:
      model: ollama/qwen2.5-coder:14b
      api_base: http://localhost:11434  # Dockerからのアクセスの場合は host.docker.internal や実IP
      supports_function_calling: true
      drop_params: true
```

---

## 4. 使い方

### ① uv による仮想環境の構築

本プロジェクトは Python パッケージマネージャ [uv](https://github.com/astral-sh/uv) を使用して仮想環境を管理します。

```bash
# 仮想環境の同期・依存関係のインストール
uv sync
```

### ② 動的モデル設定の更新

スクリプトを実行して、最新の OpenRouter / ModelGrep データから `config.yaml` を生成します。

```bash
uv run python update_config.py
```

実行に成功すると、`config.yaml` が安全（アトミック）に更新され、Discord Webhook が設定されていれば通知が送信されます。

### ③ Docker Compose による LiteLLM Proxy の起動

```bash
# コンテナの起動（バックグラウンド）
docker compose up -d

# ログの確認
docker compose logs -f litellm
```

LiteLLM Proxy が起動すると、`http://localhost:4000` で API サーバーがリッスンを開始します。

### ④ クライアントからの接続・利用方法

VS Code (Continue, Roo Code, Cline)、Zoo Code、Cursor、または Python/TypeScript SDK から以下のように接続します。

- **Base URL**: `http://<サーバーIPまたはlocalhost>:4000/v1`
- **API Key**: `.env` で設定した `LITELLM_MASTER_KEY`
- **指定可能なモデル名**:
  1. **カテゴリ代表名（自動フォールバック対応・推奨）**:
     - `popularity` : 総合人気枠 1位（障害時は 2位 → 3位 へ自動フォールバック）
     - `practical` : 高機能実用枠 1位（障害時は 2位 → 3位 へ自動フォールバック）
     - `great_deal` : 特別割引格安枠 1位（障害時は 2位 → 3位 へ自動フォールバック）
  2. **個別モデル名（名指し指定）**:
     - `popularity-<provider>-<model>` (例: `popularity-deepseek-chat`)
     - `practical-<provider>-<model>` (例: `practical-google-gemini-3.7-flash`)
     - `great_deal-<provider>-<model>` (例: `great_deal-inclusionai-ling-3.0-flash`)
  3. **ローカルモデル**:
     - `base_llm.yaml` に定義した名前（例: `local-worker-coder`）

#### curl による疎通確認例

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "popularity",
    "messages": [
      {"role": "user", "content": "Hello! What model are you?"}
    ]
  }'
```

---

## 5. 定期自動更新（cron 設定例）

定期的に `update_config.py` を実行してモデル一覧を最新化し、LiteLLM に反映させる場合は、cron に以下のように登録します。

```bash
# 毎日朝6時にモデル設定を自動更新する例
0 6 * * * cd /home/sexyroot/docker-dir/litellm && /home/sexyroot/.local/bin/uv run python update_config.py >> /tmp/update_config.log 2>&1
```

※ LiteLLM は `config.yaml` の更新を自動検知してリロードするか、コンテナ再起動で反映されます。
