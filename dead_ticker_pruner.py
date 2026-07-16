# ==============================================================
# dead_ticker_pruner.py  v1.1  (Wave R0-4 / BUG-04・BUG-05)
# デッドticker検出・除去ツール（D-14: a=削除 で決定）
# ==============================================================
# 【役割】
#   data/custom_universe/*.json 内の全tickerをyfinanceで生存確認し、
#   直近30日の価格が1件も取れない銘柄を「デッド」と判定。
#   --report : レポート表示のみ（既定）
#   --apply  : JSONから削除して保存（元ファイルは .bak_YYYYMMDD に退避）
#
# 【実行環境】ネットワーク＋yfinance必須（Mac または GitHub Actions で実行）
#   例: python3 dead_ticker_pruner.py --report
#       python3 dead_ticker_pruner.py --apply
#
# 【v1.3 / 2026-07-16】第1パスをバッチ取得化（yf.download・100銘柄/リクエスト）
#   v1.2の個別取得は1460リクエストとなりYahoo側のIPレート制限を自ら誘発していた
#   （v1.2 apply実行時に642件が再検証でも偽デッドのままガード発動＝実測）。
#   バッチ化でリクエスト数を約1/100に削減し、レート制限の発生自体を抑える。
#   第2パス（候補の個別再検証）は従来どおり。
# 【v1.2 / 2026-07-16】2パス方式（デッド候補の再検証）を標準化
#   v1.1でも防げない失敗モードが実測で判明: yfinanceは取得失敗時に例外ではなく
#   「空DataFrame＋possibly delistedログ」を返すため unknown に落ちず dead に
#   誤分類される（実害: 実行中盤のDNS断でsp500のM以降215銘柄が偽デッド）。
#   → 第1パスのデッド候補を60秒待機後に再検証し、生存していれば救済。
#   → 再検証でも候補の50%超がデッドのままなら計測異常として apply 拒否。
# 【v1.1 / 2026-07-12】判定の3値化＋計測異常セーフガード
#   v1.0は「取得例外＝デッド」だったため、yfinance未導入・ネットワーク断・
#   レート制限でも全銘柄デッドと誤報告した（実害: 2026-07-12にAAPL/MSFT含む
#   1460/1463銘柄デッドの誤レポート。原因=実行環境にyfinance未導入）。
#   ① 起動時にyfinance importを検査（無ければ即エラー終了・半端な報告をしない）
#   ② ALIVE / DEAD（正常応答かつ履歴0件）/ UNKNOWN（例外＝判定不能）を分離
#   ③ UNKNOWNは削除対象にしない
#   ④ デッド率が50%超なら計測系異常とみなし --apply を拒否
# ==============================================================

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

UNIVERSE_DIR = Path("data/custom_universe")
WAIT_SEC = 0.5   # yfinanceレート保護
DEAD_RATIO_GUARD = 0.5   # 全体デッド率がこれを超えたら計測異常として apply 拒否


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


def _batch_liveness(tickers: list) -> dict:
    """
    v1.3: 複数銘柄を1リクエストでまとめて判定する第1パス。
    Returns: {ticker: "alive" | "dead" | "unknown"}
    バッチ全体が取得失敗（例外）した場合は全員 unknown（deadにしない）。
    """
    out = {}
    try:
        import yfinance as yf
        chunk = list(tickers)
        df = yf.download(chunk, period="1mo", group_by="ticker",
                         progress=False, threads=False)
        if df is None or df.empty:
            return {t2: "unknown" for t2 in chunk}
        for t2 in chunk:
            try:
                sub = df[t2] if len(chunk) > 1 else df
                col = sub["Close"] if "Close" in sub else None
                out[t2] = "alive" if (col is not None and col.dropna().shape[0] > 0) else "dead"
            except Exception:
                out[t2] = "dead"   # バッチ応答に列が無い＝データなし（候補→第2パスで再検証）
        return out
    except Exception:
        return {t2: "unknown" for t2 in tickers}


def _check_liveness(ticker: str) -> str:
    """
    3値判定: "alive" / "dead"（正常応答かつ履歴空）/ "unknown"（例外=判定不能）。
    dead だけが削除候補。unknown を削除しないことが v1.1 の核心。
    """
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1mo")
        if h is not None and not h.empty:
            return "alive"
        return "dead"
    except Exception:
        return "unknown"


def main(apply: bool = False) -> int:
    # ① 実行環境検査: yfinance が無い環境で走らせない（全滅誤報告の再発防止）
    import importlib.util
    if importlib.util.find_spec("yfinance") is None:
        print("❌ yfinance が未導入です: pip3 install yfinance")
        print("   （この環境では生存判定ができないため中断します）")
        return 1

    if not UNIVERSE_DIR.exists():
        print(f"❌ {UNIVERSE_DIR} が見つかりません（リポジトリ直下で実行すること）")
        return 1

    stamp = datetime.now().strftime("%Y%m%d")
    total_dead = 0
    total_unknown = 0
    total_checked = 0
    report_lines = []
    dead_by_file: dict[Path, list] = {}

    for jf in sorted(UNIVERSE_DIR.glob("*.json")):
        if ".bak" in jf.name:
            continue
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            report_lines.append(f"⚠️ {jf.name}: 読込不能 ({str(e)[:50]})")
            continue
        tickers = _collect_tickers(payload)
        dead, unknown = [], []
        # v1.3: 100銘柄ずつバッチ判定（レート制限の自己誘発を防ぐ）
        for i in range(0, len(tickers), 100):
            states = _batch_liveness(tickers[i:i + 100])
            for t, state in states.items():
                if state == "dead":
                    dead.append(t)
                elif state == "unknown":
                    unknown.append(t)
            time.sleep(2.0)
        total_checked += len(tickers)
        total_dead += len(dead)
        total_unknown += len(unknown)
        if dead:
            dead_by_file[jf] = (payload, dead)
            report_lines.append(f"💀 {jf.name}: デッド{len(dead)}/{len(tickers)} → {dead}")
        if unknown:
            report_lines.append(f"❓ {jf.name}: 判定不能{len(unknown)}件（削除しない） → {unknown[:10]}"
                                + ("…" if len(unknown) > 10 else ""))
        if not dead and not unknown:
            report_lines.append(f"✅ {jf.name}: 全{len(tickers)}銘柄生存")

    # ⑤ v1.2: デッド候補の再検証パス（一過性のネットワーク断・レート制限を救済）
    all_candidates = [t2 for _, (_, ds) in dead_by_file.items() for t2 in ds]
    if all_candidates:
        print(f"\n🔁 デッド候補 {len(all_candidates)}件を60秒待機後に再検証...")
        time.sleep(60)
        confirmed_total = 0
        for jf in list(dead_by_file):
            payload, dead = dead_by_file[jf]
            confirmed, revived = [], []
            for t2 in dead:
                if _check_liveness(t2) == "alive":
                    revived.append(t2)
                else:
                    confirmed.append(t2)
                time.sleep(WAIT_SEC)
            confirmed_total += len(confirmed)
            if revived:
                report_lines.append(f"♻️ {jf.name}: 再検証で{len(revived)}件生存を確認（救済）→ {revived[:8]}"
                                    + ("…" if len(revived) > 8 else ""))
            if confirmed:
                dead_by_file[jf] = (payload, confirmed)
            else:
                del dead_by_file[jf]
        total_dead = confirmed_total
        # 再検証しても候補の大半がdeadのまま = 計測系が死んでいる疑い
        if (confirmed_total / len(all_candidates)) > DEAD_RATIO_GUARD and confirmed_total >= 30:
            report_lines.append(
                f"\n🚨 再検証後も候補の{confirmed_total}/{len(all_candidates)}件がデッドのまま。"
                "ネットワーク/レート制限の持続疑い → --apply は拒否します。")
            apply = False

    # ④ 計測異常セーフガード
    dead_ratio = (total_dead / total_checked) if total_checked else 0.0
    measurement_broken = dead_ratio > DEAD_RATIO_GUARD
    if measurement_broken:
        report_lines.append(
            f"\n🚨 デッド率 {dead_ratio:.0%}（>{DEAD_RATIO_GUARD:.0%}）は計測系異常の疑い"
            "（ネットワーク断・レート制限等）。--apply は拒否します。")
        apply = False

    if apply:
        for jf, (payload, dead) in dead_by_file.items():
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
            report_lines.append(f"   ✂️  {jf.name}: 削除して保存（退避: {jf.name}.bak_{stamp}）")

    print("\n".join(report_lines))
    print(f"\n合計: 検査{total_checked} / デッド{total_dead} / 判定不能{total_unknown}"
          f" / モード: {'apply' if apply else 'report'}"
          + ("（計測異常のためapply拒否）" if measurement_broken else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(apply="--apply" in sys.argv))
