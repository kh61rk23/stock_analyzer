#!/bin/bash
# ==============================================================
# 権益レビューUI起動.command  v1.0  (2026-07-25 バッチ5)
# exposure_review_ui.py（Streamlit）をダブルクリックで起動する
# --------------------------------------------------------------
# 【役割】
#   EDINET/SEC/JOGMEC から抽出した海外権益データを目視確認・補完・承認する
#   Streamlit UI（Wave 6 - G-C3）を、config で指定したポートで起動する。
#
# 【なぜ作ったか】
#   config.EXPOSURE_REVIEW_UI_PORT は「起動ポート」として定義されていたのに、
#   起動導線が存在せず誰にも読まれていなかった（2026-07-25 の棚卸しで判明）。
#   Streamlit のポートは実行時に --server.port で渡すしかないため、
#   config を読んで渡すこのスクリプトが定数の唯一の消費者になる。
#
# 【パス解決】
#   司令塔シェルの paths.sh を正本とする（移設に追従するため直書きしない）。
# ==============================================================
set -uo pipefail

PATHS="$HOME/Documents/Obsidian_Local/司令塔シェル_動かさない/paths.sh"
if [ ! -f "$PATHS" ]; then
  echo "❌ paths.sh が見つかりません: $PATHS" >&2
  read -p "Enter で閉じます"; exit 1
fi
source "$PATHS"

PYTHON="$(command -v python3 || echo /usr/bin/python3)"
APP_REL="exposure_review_ui.py"

if [ ! -f "$STOCK/$APP_REL" ]; then
  echo "❌ アプリ本体が見つかりません: $STOCK/$APP_REL" >&2
  read -p "Enter で閉じます"; exit 1
fi

cd "$STOCK" || { echo "❌ フォルダへ移動できません"; read -p "Enter で閉じます"; exit 1; }

# config からポートを取得（読めなければ既定 8502）
PORT="$("$PYTHON" - <<'PY' 2>/dev/null || echo 8502
import importlib.util
spec = importlib.util.spec_from_file_location("config", "config.py")
cfg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfg)
print(int(getattr(cfg, "EXPOSURE_REVIEW_UI_PORT", 8502)))
PY
)"
echo "=== 権益マッピング レビューUI を起動します（port=$PORT）==="

if command -v streamlit >/dev/null 2>&1; then
  RUN=(streamlit run "$APP_REL" --server.port "$PORT")
elif "$PYTHON" -m streamlit --version >/dev/null 2>&1; then
  RUN=("$PYTHON" -m streamlit run "$APP_REL" --server.port "$PORT")
else
  echo "⚠ streamlit が見つかりません。次のコマンドで導入してください:"
  echo "    pip3 install -r requirements.txt --break-system-packages"
  read -p "Enter で閉じます"; exit 1
fi

echo "▶ 実行: ${RUN[*]}"
echo "   ブラウザが自動で開きます（http://localhost:$PORT ）。"
echo "   終了は Ctrl + C。"
exec "${RUN[@]}"
