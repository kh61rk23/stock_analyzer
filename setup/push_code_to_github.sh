#!/bin/bash
# ==============================================================
# push_code_to_github.sh — ローカルのコード改修だけを GitHub へ反映
# ==============================================================
# 【目的】
#   ローカル「更新用」フォルダのコード（*.py / .github / config.py /
#   setup / data/custom_universe 等）を GitHub リポジトリへ push する。
#   GitHub Actions が毎朝自動 commit している stock_data/（ダッシュボード・
#   キャッシュ等の実行結果データ）は temp 取得した最新リポジトリ側を温存し、
#   上書きしない。
#
# 【使い方】Mac のターミナルで実行（1回／改修のたび）:
#   cd "/Users/kentaryobe/Documents/株・資産系統/株システム関連/stock_analyzer/stock_analyzer-main（更新用）"
#   bash setup/push_code_to_github.sh
#   （URL を変えたい場合）bash setup/push_code_to_github.sh https://github.com/kh61rk23/stock_analyzer.git
#
# 【前提】git 認証が済んでいること。未認証なら下記のどちらか:
#   - GitHub CLI:  gh auth login
#   - もしくは Personal Access Token を git credential helper に登録
#
# 【安全設計】
#   - stock_data/ を rsync 対象から除外 = Actions 生成データを破壊しない
#   - --delete を使わない = 想定外のファイル削除を起こさない
#     （ローカルで削除したファイルはリポジトリに残る。意図的削除時は手動で）
# ==============================================================
set -euo pipefail

REPO_URL="${1:-https://github.com/kh61rk23/stock_analyzer.git}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"   # 更新用フォルダの絶対パス
WORK="$HOME/.stock_analyzer_push"          # 作業用クローン置き場

echo "▶ リポジトリ : $REPO_URL"
echo "▶ 改修元(SRC): $SRC"
echo "▶ 作業ディレクトリ: $WORK"

# 1. 最新リポジトリを取得（Actions 生成データ込みの最新 main に揃える）
if [ -d "$WORK/.git" ]; then
  git -C "$WORK" remote set-url origin "$REPO_URL"
  git -C "$WORK" fetch origin
  git -C "$WORK" checkout main
  git -C "$WORK" reset --hard origin/main
else
  rm -rf "$WORK"
  git clone "$REPO_URL" "$WORK"
fi

# 2. コードのみ上書きコピー（stock_data/ と各種キャッシュは除外）
rsync -av \
  --exclude '.git/' \
  --exclude 'stock_data/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'pytest_cache/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$SRC"/ "$WORK"/

# 3. コミット＆プッシュ
cd "$WORK"
git add -A
if git diff --cached --quiet; then
  echo "⏩ 変更なし（push 不要）"
else
  git commit -m "code: ローカル改修反映 $(date '+%Y-%m-%d %H:%M')"
  git push origin main
  echo "✅ push 完了"
fi
