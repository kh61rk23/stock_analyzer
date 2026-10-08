# ==============================================================
# timeline_manager.py  v1.0  (3-A)
# 二時間軸分析マネージャー
# ==============================================================
# 長期軸: 業種代表銘柄の月次終値 × N ヶ月MA → 転換点検出
#         → insights/macro/monthly_trend_YYYY-MM.md
# 短期軸: 業種代表銘柄の前日比変化率 → 業種別騰落ランキング
#         → insights/macro/daily_movers_YYYY-MM-DD.md
# 依存: config.py のみ / yfinance, pandas
# 呼出: result = run_timeline_analysis(cfg)
# ==============================================================

from datetime import datetime, timedelta
from pathlib import Path
import time


# ==============================================================
# BLOCK 1: データ取得
# ==============================================================

def fetch_monthly_prices(tickers: list, months: int = 12, max_retries: int = 2):
    """月次終値 DataFrame を返す（列=ticker, 行=月次日付）。失敗時 None。"""
    try:
        import yf_cache as yf
        import pandas as pd
    except ImportError:
        print("  ⚠️  yfinance/pandas 未インストール"); return None

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=months * 35)

    for attempt in range(1, max_retries + 1):
        try:
            raw = yf.download(
                tickers,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval="1mo",
                auto_adjust=True,
                progress=False,
                timeout=30,
            )
            if raw is None or raw.empty:
                time.sleep(3); continue
            import pandas as pd
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
                    else raw[["Close"]].rename(columns={"Close": tickers[0]})
            return close.tail(months)
        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠️  月次取得失敗: {str(e)[:60]}"); return None
            time.sleep(3)
    return None


def fetch_daily_prices(tickers: list, days: int = 5, max_retries: int = 2):
    """直近 N+10 日の日次終値 DataFrame を返す。失敗時 None。"""
    try:
        import yf_cache as yf
        import pandas as pd
    except ImportError:
        return None

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days + 10)

    for attempt in range(1, max_retries + 1):
        try:
            raw = yf.download(
                tickers,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
                progress=False,
                timeout=30,
            )
            if raw is None or raw.empty:
                time.sleep(2); continue
            import pandas as pd
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
                    else raw[["Close"]].rename(columns={"Close": tickers[0]})
            return close.dropna(how="all")
        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠️  日次取得失敗: {str(e)[:60]}"); return None
            time.sleep(2)
    return None


# ==============================================================
# BLOCK 2: 長期トレンド分析
# ==============================================================

def detect_trend_changes(prices_df, window: int = 3, threshold: float = 0.02) -> dict:
    """
    月次終値に N ヶ月 MA を適用しトレンドと転換点を返す。

    ゴールデンクロス: 前月 MA 下 → 今月 MA 上
    デッドクロス    : 前月 MA 上 → 今月 MA 下
    乖離率 = (価格 - MA) / MA × 100

    Returns:
        {ticker: {latest_price, latest_ma, deviation_pct, trend, cross}}
    """
    ma_df = prices_df.rolling(window=window, min_periods=window).mean()
    out   = {}

    for ticker in prices_df.columns:
        try:
            p = prices_df[ticker].dropna()
            m = ma_df[ticker].dropna()
            if len(p) < 2 or len(m) < 2:
                continue
            lp, lm = float(p.iloc[-1]), float(m.iloc[-1])
            pp, pm = float(p.iloc[-2]), float(m.iloc[-2])
            if lm == 0 or pm == 0:
                continue

            dev = (lp - lm) / lm * 100
            trend = "up" if dev > threshold * 100 else \
                    ("down" if dev < -threshold * 100 else "flat")

            was_above = pp > pm
            is_above  = lp > lm
            cross = "golden" if (not was_above and is_above) else \
                    ("dead"   if (was_above and not is_above) else None)

            out[ticker] = {
                "latest_price":  round(lp, 2),
                "latest_ma":     round(lm, 2),
                "deviation_pct": round(dev, 2),
                "trend":  trend,
                "cross":  cross,
            }
        except Exception:
            continue
    return out


def run_long_term_analysis(cfg) -> dict:
    """全業種代表銘柄で長期月次トレンド分析を実行する。"""
    months    = getattr(cfg, "LONG_TERM_MONTHS",       12)
    ma_window = getattr(cfg, "LONG_TERM_MA_WINDOW",     3)
    threshold = getattr(cfg, "TREND_CHANGE_THRESHOLD", 0.02)
    rep       = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})

    sector_trends = []
    golden_cross  = []
    dead_cross    = []

    print(f"\n  📈 長期トレンド分析（{months}ヶ月 / MA{ma_window}ヶ月）...")

    for code, tickers in rep.items():
        if not tickers:
            continue
        name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
        print(f"    [{code}] {name} ...")

        prices = fetch_monthly_prices(tickers, months=months)
        if prices is None or prices.empty:
            print(f"      ⚠️  データなし（スキップ）"); continue

        detail = detect_trend_changes(prices, window=ma_window, threshold=threshold)
        if not detail:
            continue

        ups   = sum(1 for v in detail.values() if v["trend"] == "up")
        downs = sum(1 for v in detail.values() if v["trend"] == "down")
        trend_summary = "up" if ups > downs else ("down" if downs > ups else "flat")

        cross_flags = []
        has_gc = has_dc = False
        for tkr, d in detail.items():
            if d["cross"] == "golden":
                cross_flags.append(f"{tkr}: ⚡ ゴールデンクロス（MA上抜け）")
                has_gc = True
            elif d["cross"] == "dead":
                cross_flags.append(f"{tkr}: 💀 デッドクロス（MA下抜け）")
                has_dc = True
        if has_gc: golden_cross.append({"sector_code": code, "sector_name": name})
        if has_dc: dead_cross.append({"sector_code": code, "sector_name": name})

        sector_trends.append({
            "sector_code":    code,
            "sector_name":    name,
            "trend_summary":  trend_summary,
            "cross_flags":    cross_flags,
            "ticker_details": detail,
        })
        time.sleep(0.3)

    return {
        "sector_trends":        sector_trends,
        "golden_cross_sectors": golden_cross,
        "dead_cross_sectors":   dead_cross,
        "analysis_month":       datetime.now().strftime("%Y-%m"),
    }


# ==============================================================
# BLOCK 3: 短期デイリー分析
# ==============================================================

def run_short_term_analysis(cfg) -> list:
    """全業種代表銘柄の前日比変化率を計算し業種別ランキングを返す（降順）。"""
    rep     = getattr(cfg, "SECTOR_REPRESENTATIVE_TICKERS", {})
    results = []

    print(f"\n  📉 短期デイリー分析（{len(rep)} 業種）...")

    for code, tickers in rep.items():
        if not tickers:
            continue
        name = cfg.JAPAN_SECTORS.get(code, {}).get("name_ja", code)
        print(f"    [{code}] {name} ...")

        # 取得日数は config.SHORT_TERM_DAYS から読む（直書きしない）。
        prices = fetch_daily_prices(
            tickers, days=int(getattr(cfg, "SHORT_TERM_DAYS", 5)))
        if prices is None or len(prices) < 2:
            print(f"      ⚠️  データ不足（スキップ）"); continue

        changes = {}
        for tkr in prices.columns:
            col = prices[tkr].dropna()
            if len(col) >= 2:
                chg = (float(col.iloc[-1]) - float(col.iloc[-2])) / float(col.iloc[-2]) * 100
                changes[tkr] = round(chg, 2)
        if not changes:
            continue

        avg_chg = round(sum(changes.values()) / len(changes), 2)
        best    = max(changes, key=changes.get)
        worst   = min(changes, key=changes.get)

        results.append({
            "sector_code":    code,
            "sector_name":    name,
            "avg_change_pct": avg_chg,
            "best_ticker":    best,
            "best_change":    changes[best],
            "worst_ticker":   worst,
            "worst_change":   changes[worst],
            "ticker_count":   len(changes),
            "all_changes":    changes,
        })
        time.sleep(0.3)

    results.sort(key=lambda r: r["avg_change_pct"], reverse=True)
    return results


# ==============================================================
# BLOCK 4: メインエントリポイント
# ==============================================================

def run_timeline_analysis(cfg) -> dict:
    """長期・短期の両軸を実行し結果辞書を返す。"""
    print("\n  ⏳ タイムライン分析 開始...")
    t0 = datetime.now()

    long_term  = run_long_term_analysis(cfg)  if getattr(cfg, "ENABLE_TIMELINE_LONG",  True) else {}
    short_term = run_short_term_analysis(cfg) if getattr(cfg, "ENABLE_TIMELINE_SHORT", True) else []

    if not getattr(cfg, "ENABLE_TIMELINE_LONG",  True):
        print("  ⏩ 長期トレンド: スキップ（ENABLE_TIMELINE_LONG=False）")
    if not getattr(cfg, "ENABLE_TIMELINE_SHORT", True):
        print("  ⏩ 短期デイリー: スキップ（ENABLE_TIMELINE_SHORT=False）")

    elapsed = (datetime.now() - t0).seconds
    print(f"\n  ✅ タイムライン分析完了（{elapsed}秒）")

    result = {
        "long_term":  long_term,
        "short_term": short_term,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_timeline_report(result, cfg)
    return result


# ==============================================================
# BLOCK 5: Markdown レポート保存
# ==============================================================

def _save_timeline_report(result: dict, cfg) -> None:
    """長期/短期レポートを insights/macro/ に保存する。"""
    today     = datetime.now().strftime("%Y-%m-%d")
    month     = datetime.now().strftime("%Y-%m")
    macro_dir = Path(getattr(cfg, "MACRO_DIR", ""))
    if not str(macro_dir):
        return

    try:
        macro_dir.mkdir(parents=True, exist_ok=True)

        # 長期レポート
        lt = result.get("long_term", {})
        if lt and lt.get("sector_trends"):
            lines = [
                f"# 長期トレンド レポート（{month}）", "",
                f"**ゴールデンクロス**: {len(lt.get('golden_cross_sectors',[]))}業種 / "
                f"**デッドクロス**: {len(lt.get('dead_cross_sectors',[]))}業種", "",
            ]
            crossed = [s for s in lt["sector_trends"] if s.get("cross_flags")]
            if crossed:
                lines += ["## ⚡ 転換点検出業種", ""]
                for s in crossed:
                    icon = "📈" if s["trend_summary"] == "up" else "📉"
                    lines.append(f"### {icon} {s['sector_name']} `{s['sector_code']}`")
                    for flag in s["cross_flags"]:
                        lines.append(f"- {flag}")
                    lines.append("")
            lines += ["## 全業種 トレンドサマリー", "",
                      "| 業種 | コード | トレンド | 代表銘柄乖離率 |",
                      "|------|--------|---------|--------------|"]
            for s in lt["sector_trends"]:
                ti   = {"up":"📈 上昇","down":"📉 下落","flat":"➡️ 横ばい"}.get(s["trend_summary"],"⚪")
                devs = ", ".join(f"{t}: {v['deviation_pct']:+.1f}%"
                                 for t,v in list(s.get("ticker_details",{}).items())[:2])
                lines.append(f"| {s['sector_name']} | {s['sector_code']} | {ti} | {devs} |")
            (macro_dir / f"monthly_trend_{month}.md").write_text(
                "\n".join(lines), encoding="utf-8")
            print(f"    💾 長期レポート: monthly_trend_{month}.md")

        # 短期レポート
        st_data = result.get("short_term", [])
        if st_data:
            lines = [
                f"# 業種別デイリー騰落 レポート（{today}）", "",
                "| 順位 | 業種 | 平均騰落率 | ベスト銘柄 | ワースト銘柄 |",
                "|------|------|-----------|-----------|------------|",
            ]
            for i, s in enumerate(st_data, 1):
                best  = f"{s['best_ticker']} ({s['best_change']:+.2f}%)"
                worst = f"{s['worst_ticker']} ({s['worst_change']:+.2f}%)"
                lines.append(f"| {i} | {s['sector_name']} | {s['avg_change_pct']:+.2f}% "
                              f"| {best} | {worst} |")
            (macro_dir / f"daily_movers_{today}.md").write_text(
                "\n".join(lines), encoding="utf-8")
            print(f"    💾 短期レポート: daily_movers_{today}.md")

    except Exception as e:
        print(f"    ⚠️  レポート保存失敗: {str(e)[:60]}")


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_timeline_analysis(cfg)
    lt = result.get("long_term", {})
    print(f"\n長期: {len(lt.get('sector_trends',[]))} 業種")
    print(f"  ゴールデンクロス: {lt.get('golden_cross_sectors', [])}")
    print(f"  デッドクロス    : {lt.get('dead_cross_sectors',   [])}")
    print(f"短期: {len(result.get('short_term', []))} 業種")
