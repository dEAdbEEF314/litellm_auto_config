#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${BASE_DIR}/logs"
CURRENT_MONTH="$(date +'%Y-%m')"
CURRENT_LOG="${LOG_DIR}/update_${CURRENT_MONTH}.log"

mkdir -p "${LOG_DIR}"

# スクリプト全体の標準出力・標準エラーを月別ログへ追記出力
exec > >(tee -a "${CURRENT_LOG}") 2>&1

echo "=== LiteLLM Update Started: $(date '+%Y-%m-%d %H:%M:%S') ==="

cd "${BASE_DIR}"

# 設定ファイルの自動生成
uv run update_config.py

# コンテナ再起動
docker compose restart

echo "=== LiteLLM Update Finished: $(date '+%Y-%m-%d %H:%M:%S') ==="

# --- ログのローテーションと圧縮 ---
# 当月以外の未圧縮ログ（update_*.log）があれば gzip 圧縮
find "${LOG_DIR}" -maxdepth 1 -type f -name "update_*.log" ! -name "update_${CURRENT_MONTH}.log" -exec gzip -f {} +

# 古い圧縮ログ（180日以上経過）を自動削除
find "${LOG_DIR}" -maxdepth 1 -type f -name "update_*.log.gz" -mtime +180 -delete
