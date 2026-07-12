# ==============================================================
# dead_ticker_pruner.py  v1.0  (Wave R0-4 / BUG-04・BUG-05)
# デッドticker検出・除去ツール（D-14: a=削除 で決定）
# ==============================================================
# 【役割】
#   data/custom_universe/*.json 内の全tickerをyfinanceで生存確認し、
#   直近30日の価格が1件も取れない銘柄を「デッド」と判定。
#   --report : レポート表示のみ（既定）
#   --apply  : JSONから削除して保存（元ファイルは .bak_YYYYMMDD に退避）
#
# 【実行環境】ネットワーク必須（Mac または GitHub Actions で実行）
#   例: python3 dead_ticker_pruner.py --report
#       python3 dead_ticker_pruner.py --apply
# 【備考】BUG-04（5953.T）は2026-07-05時点でリポジトリ内に参照が
#   見つからず解消済みの可能性が高い。本ツールで最終確認すること。
# ==============================================================

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

UNIVERSE_DIR = Path("data/custom_universe")
WAIT_SEC = 0.5   # yfinanceレート保護


def _collect_tickers(payload) -> list:
    """JSON構造の揺れ（list / {"tickers": [...]} / dict値）に耐えるticker抽出。"""
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, str)]
    if isinstance(payload, dict):
        for key in ("tickers", "symbols", "universe"):
            if isinstance(payload.get(key), list):
                return [t for t in payload[key] if isinstance(t, str)]
        # dict の値がリストの場合は結合
        out = []
        for v in payload.values():
            if isinstance(v, list):
                out.extend(t for t in v if isinstance(t, str))
        return out
    return []


def _is_alive(ticker: str) -> bool:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1mo")
        return h is not None and not h.empty
    except Exception:
        return False


def main(apply: bool = False) -> int:
    if not UNIVERSE_DIR.exists():
        print(f"❌ {UNIVERSE_DIR} が見つかりません（リポジトリ直下で実行すること）")
        return 1

    stamp = datetime.now().strftime("%Y%m%d")
    total_dead = 0
    report_lines = []

    for jf in sorted(UNIVERSE_DIR.glob("*.json")):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            report_lines.append(f"⚠️ {jf.name}: 読込不能 ({str(e)[:50]})")
            continue
        tickers = _collect_tickers(payload)
        dead = []
        for t in tickers:
            if not _is_alive(t):
                dead.append(t)
            time.sleep(WAIT_SEC)
        if dead:
            total_dead += len(dead)
            report_lines.append(f"💀 {jf.name}: {len(dead)}/{len(tickers)}銘柄デッド → {dead}")
            if apply:
                jf.rename(jf.with_suffix(f".json.bak_{stamp}"))
                if isinstance(payload, list):
                    new_payload = [t for t in payload if t not in dead]
                else:
                    new_payload = payload
                    for key in ("tickers", "symbols", "universe"):
                        if isinstance(payload.get(key), list):
                            new_payload[key] = [t for t in payload[key] if t not in dead]
                jf.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
                report_lines.append(f"   ✂️  削除して保存（退避: {jf.name}.bak_{stamp}）")
        else:
            report_lines.append(f"✅ {jf.name}: 全{len(tickers)}銘柄生存")

    print("\n".join(report_lines))
    print(f"\n合計デッド: {total_dead}件 / モード: {'apply' if apply else 'report'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(apply="--apply" in sys.argv))
