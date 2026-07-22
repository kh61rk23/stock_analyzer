#!/bin/bash
# ==============================================================
# install_launchd.sh — obsidian_sync 日次実行の launchd 登録
# ==============================================================
# 使い方（Macのターミナルで1回だけ実行）:
#   bash setup/install_launchd.sh https://github.com/<あなた>/<リポジトリ>.git
#
# やること:
#   1. plist の REPO_URL / リポジトリパスを埋めて
#      ~/Library/LaunchAgents/ にコピー
#   2. launchctl で登録（毎朝10:30に obsidian_sync.py all が走る）
# 解除したい場合:
#   launchctl unload ~/Library/LaunchAgents/com.kentaryobe.stock-analyzer.obsidian-sync.plist
# ==============================================================
set -euo pipefail

REPO_URL="${1:-}"
if [ -z "$REPO_URL" ]; then
  echo "❌ 使い方: bash setup/install_launchd.sh <GitHubリポジトリURL>"
  echo "   例: bash setup/install_launchd.sh https://github.com/kentaryobe/stock_analyzer.git"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.kentaryobe.stock-analyzer.obsidian-sync"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__REPO_URL__|$REPO_URL|g" \
    "$SCRIPT_DIR/$LABEL.plist" > "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "✅ 登録完了: 毎朝10:30に obsidian_sync.py all が実行されます"
echo "   ログ: /tmp/stock-analyzer-obsidian-sync.log"
echo "   手動テスト: launchctl start $LABEL"
