# ==============================================================
# policy_tracker.py  v1.0  (3-C)
# 日銀・FRB 金融政策変更の自動検知
# ==============================================================
# 【役割】
#   日銀・FRBの政策金利変化を市場データから自動検知し、
#   変化イベントを蓄積・レポート出力する。
#
# 【検知ロジック】
#   yfinance で下記4指標を取得し「前回キャッシュ値」と比較する。
#     ^IRX      : 米3ヶ月T-bill利回り（FFレートのプロキシ）
#     ^TNX      : 米10年債利回り
#     ^JP10YT=RR: 日本10年債利回り
#     JPY=X     : USD/JPY（円安・円高を政策の副作用として監視）
#   POLICY_CHANGE_THRESHOLD(%pt) 以上の変動を「変化イベント」として記録。
#
#   オプション: POLICY_FRED_API_KEY が設定されている場合、
#     FRED API から FEDFUNDS（FFレート実績値）・IORB を取得して補完。
#     ※ 環境のネットワークが有効な場合のみ機能する。
#
# 【出力ファイル】
#   DATA_DIR/policy_cache.json      : 最新値キャッシュ（比較用）
#   DATA_DIR/policy_events.json     : 変化イベント累積ログ
#   insights/macro/policy_changes_YYYY-MM.md : 月次レポート
#
# 【返り値スキーマ】run_policy_tracker(cfg) の返り値
#   {
#     "new_events":    [...],      # 今回検出した新規イベント
#     "all_events":    [...],      # 全累積イベント
#     "current_rates": {...},      # 最新レート値
#     "rate_history":  {...},      # 直近30日の推移 {ticker: [{date,value},...]}
#     "timestamp":     "...",
#   }
#   イベント要素:
#   {
#     "detected_at":   "2026-02-27 08:00:00",
#     "ticker":        "^IRX",
#     "label":         "米FFレート(proxy)",
#     "prev_value":    5.25,
#     "curr_value":    5.00,
#     "change_pt":     -0.25,
#     "direction":     "緩和方向（利下げ）",
#     "sector_impact": ["銀行業: 逆風", "不動産: 追い風", ...],
#   }
#
# 【依存】config.py / yfinance / pandas / json
# 【呼出】from policy_tracker import run_policy_tracker
# ==============================================================

from datetime import datetime, timedelta
from pathlib import Path
import json
import time


# ==============================================================
# BLOCK 1: 監視対象の定義
# ==============================================================

# 監視対象指標（ticker → メタ情報）
WATCH_TICKERS = {
    "^IRX": {
        "label":    "米FFレート(3M T-bill proxy)",
        "bank":     "FRB",
        "currency": "USD",
        # 上昇 → 引き締め / 下落 → 緩和
        "up_dir":   "引き締め方向（利上げ）",
        "dn_dir":   "緩和方向（利下げ）",
        # 引き締め時に追い風・逆風となる業種コード（config.JAPAN_SECTORS のキー）
        "up_tailwind": ["1028"],         # 銀行
        "up_headwind": ["1032", "1017"], # 不動産・輸送用機器
        "dn_tailwind": ["1032"],         # 不動産
        "dn_headwind": ["1028"],         # 銀行
    },
    "^TNX": {
        "label":    "米10年債利回り",
        "bank":     "FRB/市場",
        "currency": "USD",
        "up_dir":   "長期金利上昇（引き締め期待 or インフレ）",
        "dn_dir":   "長期金利低下（景気後退懸念 or 緩和期待）",
        "up_tailwind": ["1028"],
        "up_headwind": ["1032"],
        "dn_tailwind": ["1032"],
        "dn_headwind": ["1028"],
    },
    "^JP10YT=RR": {
        "label":    "日本10年債利回り",
        "bank":     "日銀",
        "currency": "JPY",
        "up_dir":   "日銀 利上げ方向（YCC 撤廃・引き締め）",
        "dn_dir":   "日銀 緩和継続・利下げ方向",
        "up_tailwind": ["1028"],
        "up_headwind": ["1032"],
        "dn_tailwind": ["1032"],
        "dn_headwind": ["1028"],
    },
    "JPY=X": {
        "label":    "USD/JPY（円安↑ 円高↓）",
        "bank":     "為替",
        "currency": "JPY",
        "up_dir":   "円安進行（輸出業に追い風）",
        "dn_dir":   "円高進行（輸入業・内需に恩恵）",
        "up_tailwind": ["1017", "1022", "1016"],  # 自動車・海運・電子
        "up_headwind": ["1004"],                   # 食品（輸入コスト増）
        "dn_tailwind": ["1004"],
        "dn_headwind": ["1017", "1022"],
    },
}

# FRED API エンドポイントと取得系列
FRED_SERIES = {
    "FEDFUNDS": {"label": "FFレート実績値(%)",  "bank": "FRB"},
    "IORB":     {"label": "超過準備金利率(%)",  "bank": "FRB"},
    "BOJRATE":  {"label": "日銀政策金利(%)",    "bank": "日銀"},
}


# ==============================================================
# BLOCK 2: yfinance データ取得
# ==============================================================

def fetch_rate_data(days: int = 90, max_retries: int = 2):
    """
    監視対象全ティッカーの日次終値を一括取得する。

    Returns:
        pd.DataFrame | None  ※ 列=ticker, 行=日付
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("  ⚠️  yfinance/pandas 未インストール")
        return None

    tickers  = list(WATCH_TICKERS.keys())
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
                time.sleep(3)
                continue

            import pandas as pd
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) \
                    else raw[["Close"]].rename(columns={"Close": tickers[0]})
            return close.dropna(how="all").tail(days)

        except Exception as e:
            if attempt == max_retries:
                print(f"    ⚠️  レートデータ取得失敗: {str(e)[:60]}")
                return None
            time.sleep(3)
    return None


# ==============================================================
# BLOCK 3: FRED API 取得（オプション）
# ==============================================================

def fetch_fred_data(api_key: str, series_id: str, days: int = 90) -> list:
    """
    FRED API から時系列データを取得する。
    ネットワークが無効または API キーが未設定の場合は [] を返す。

    Returns:
        [{"date": "YYYY-MM-DD", "value": float}, ...]
    """
    if not api_key:
        return []

    try:
        import urllib.request
        import json as _json

        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}"
            f"&observation_start={start_dt.strftime('%Y-%m-%d')}"
            f"&observation_end={end_dt.strftime('%Y-%m-%d')}"
            f"&api_key={api_key}"
            f"&file_type=json"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = _json.loads(resp.read())
        observations = data.get("observations", [])
        result = []
        for obs in observations:
            try:
                result.append({
                    "date":  obs["date"],
                    "value": float(obs["value"]),
                })
            except (ValueError, KeyError):
                pass
        return result

    except Exception as e:
        # ネットワーク無効時も静かに空リストを返す
        return []


# ==============================================================
# BLOCK 4: キャッシュ管理
# ==============================================================

def load_cache(cache_path: Path) -> dict:
    """前回キャッシュを読み込む。存在しない場合は {}。"""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    """最新値をキャッシュに保存する。"""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  キャッシュ保存失敗: {str(e)[:50]}")


def load_events(events_path: Path) -> list:
    """過去イベントログを読み込む。存在しない場合は []。"""
    if events_path.exists():
        try:
            return json.loads(events_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_events(events_path: Path, events: list) -> None:
    """イベントログを保存する（上限 500 件）。"""
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = events[-500:]
        events_path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  イベントログ保存失敗: {str(e)[:50]}")


# ==============================================================
# BLOCK 5: 変化検知・イベント生成
# ==============================================================

def _sector_impact_text(meta: dict, direction: str, sector_names: dict) -> list:
    """
    政策変化方向に応じた業種影響コメントリストを返す。

    Args:
        meta      : WATCH_TICKERS の1エントリ
        direction : "up" | "dn"
        sector_names: {code: name} の辞書

    Returns:
        ["銀行業: 📈 追い風", "不動産: 📉 逆風", ...]
    """
    tailwind_codes = meta.get(f"{direction}_tailwind", [])
    headwind_codes = meta.get(f"{direction}_headwind", [])
    impacts = []
    for code in tailwind_codes:
        name = sector_names.get(code, code)
        impacts.append(f"{name}: 📈 追い風")
    for code in headwind_codes:
        name = sector_names.get(code, code)
        impacts.append(f"{name}: 📉 逆風")
    return impacts


def detect_changes(
    prices_df,
    cache:     dict,
    threshold: float,
    sector_names: dict,
) -> list:
    """
    最新値とキャッシュ値を比較してイベントリストを生成する。

    Args:
        prices_df    : fetch_rate_data() の返り値
        cache        : {ticker: {"date": ..., "value": ...}}
        threshold    : 変化閾値（%pt）
        sector_names : {sector_code: name_ja}

    Returns:
        [event_dict, ...]  変化が検出されたティッカーのみ
    """
    import pandas as pd
    events       = []
    now_str      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latest_date  = prices_df.index[-1]

    for ticker, meta in WATCH_TICKERS.items():
        if ticker not in prices_df.columns:
            continue

        col  = prices_df[ticker].dropna()
        if col.empty:
            continue

        curr_val  = round(float(col.iloc[-1]), 4)
        curr_date = col.index[-1].strftime("%Y-%m-%d")

        # キャッシュが空の場合は現在値を初期値として登録して次回へ
        if ticker not in cache:
            cache[ticker] = {"date": curr_date, "value": curr_val}
            continue

        prev_val  = cache[ticker]["value"]
        prev_date = cache[ticker]["date"]
        change_pt = round(curr_val - prev_val, 4)

        if abs(change_pt) < threshold:
            continue  # 閾値未満の変化は無視

        direction = "up" if change_pt > 0 else "dn"
        dir_text  = meta["up_dir"] if direction == "up" else meta["dn_dir"]
        impacts   = _sector_impact_text(meta, direction, sector_names)

        events.append({
            "detected_at":   now_str,
            "ticker":        ticker,
            "label":         meta["label"],
            "bank":          meta["bank"],
            "prev_date":     prev_date,
            "curr_date":     curr_date,
            "prev_value":    prev_val,
            "curr_value":    curr_val,
            "change_pt":     change_pt,
            "direction":     dir_text,
            "sector_impact": impacts,
        })

        # キャッシュを最新値で更新
        cache[ticker] = {"date": curr_date, "value": curr_val}

    return events


# ==============================================================
# BLOCK 6: 直近推移データ作成
# ==============================================================

def build_rate_history(prices_df, history_days: int = 30) -> dict:
    """
    直近 history_days 日の推移を ticker ごとに返す。

    Returns:
        {ticker: [{"date": "YYYY-MM-DD", "value": float}, ...]}
    """
    result = {}
    recent = prices_df.tail(history_days)

    for ticker in WATCH_TICKERS:
        if ticker not in recent.columns:
            continue
        col = recent[ticker].dropna()
        result[ticker] = [
            {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
            for d, v in col.items()
        ]
    return result


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_policy_tracker(cfg) -> dict:
    """
    政策金利変化の検知を実行して結果辞書を返す。
    失敗時は {} を返し、例外を上位に伝播しない。
    """
    print("\n  🏦 政策金利トラッカー 開始...")
    t0 = datetime.now()

    data_dir   = Path(getattr(cfg, "DATA_DIR",               "data"))
    macro_dir  = Path(getattr(cfg, "MACRO_DIR",              ""))
    threshold  = getattr(cfg, "POLICY_CHANGE_THRESHOLD",      0.05)
    fred_key   = getattr(cfg, "POLICY_FRED_API_KEY",         "")
    history_d  = 30

    # sector_code → name_ja の辞書を事前構築
    sector_names = {
        code: info.get("name_ja", code)
        for code, info in getattr(cfg, "JAPAN_SECTORS", {}).items()
    }

    # --- yfinance でレートデータ取得 ---
    print(f"    📡 レートデータ取得中...")
    prices_df = fetch_rate_data(days=90)
    if prices_df is None or prices_df.empty:
        print("    ⚠️  レートデータ取得失敗")
        return {}

    # --- FRED 補完（任意）---
    fred_data = {}
    if fred_key:
        print(f"    📡 FRED データ取得中...")
        for sid, meta in FRED_SERIES.items():
            records = fetch_fred_data(fred_key, sid, days=90)
            if records:
                fred_data[sid] = records
                print(f"      → {sid}: {len(records)} 件")
    else:
        print("    ⏩ FRED API: キー未設定（yfinance データのみ使用）")

    # --- キャッシュ読み込み ---
    cache_path  = data_dir / "policy_cache.json"
    events_path = data_dir / "policy_events.json"
    data_dir.mkdir(parents=True, exist_ok=True)

    cache     = load_cache(cache_path)
    all_events = load_events(events_path)

    # --- 変化検知 ---
    print(f"    🔍 変化検知中（閾値: {threshold}%pt）...")
    new_events = detect_changes(prices_df, cache, threshold, sector_names)

    if new_events:
        print(f"    ⚡ 新規イベント: {len(new_events)} 件")
        for ev in new_events:
            print(f"      [{ev['bank']}] {ev['label']}: "
                  f"{ev['prev_value']:.3f} → {ev['curr_value']:.3f} "
                  f"({ev['change_pt']:+.3f}%pt) {ev['direction']}")
        all_events.extend(new_events)
        save_events(events_path, all_events)
    else:
        print(f"    ✅ 有意な変化なし（閾値 {threshold}%pt 未満）")

    # --- キャッシュ保存 ---
    save_cache(cache_path, cache)

    # --- 直近推移データ ---
    rate_history  = build_rate_history(prices_df, history_days=history_d)

    # --- 現在レート ---
    current_rates = {}
    for ticker, meta in WATCH_TICKERS.items():
        if ticker in prices_df.columns:
            col = prices_df[ticker].dropna()
            if not col.empty:
                current_rates[ticker] = {
                    "label": meta["label"],
                    "bank":  meta["bank"],
                    "value": round(float(col.iloc[-1]), 4),
                    "date":  col.index[-1].strftime("%Y-%m-%d"),
                }

    # FRED 最新値を補完
    for sid, records in fred_data.items():
        if records:
            last = records[-1]
            current_rates[sid] = {
                "label": FRED_SERIES[sid]["label"],
                "bank":  FRED_SERIES[sid]["bank"],
                "value": last["value"],
                "date":  last["date"],
            }

    elapsed = (datetime.now() - t0).seconds
    print(f"\n  ✅ 政策金利トラッカー完了（{elapsed}秒）")

    result = {
        "new_events":    new_events,
        "all_events":    all_events[-50:],  # 直近50件を UI に渡す
        "current_rates": current_rates,
        "rate_history":  rate_history,
        "fred_data":     fred_data,
        "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_policy_report(result, cfg)
    return result


# ==============================================================
# BLOCK 8: Markdown レポート保存
# ==============================================================

def _save_policy_report(result: dict, cfg) -> None:
    """insights/macro/policy_changes_YYYY-MM.md に保存する。"""
    month     = datetime.now().strftime("%Y-%m")
    macro_dir = Path(getattr(cfg, "MACRO_DIR", ""))
    if not str(macro_dir):
        return

    try:
        macro_dir.mkdir(parents=True, exist_ok=True)
        threshold = getattr(cfg, "POLICY_CHANGE_THRESHOLD", 0.05)

        lines = [
            f"# 政策金利変化 レポート（{month}）", "",
            f"**検知閾値**: {threshold}%pt / "
            f"**取得時刻**: {result.get('timestamp', '')}", "",
            "## 現在レート", "",
            "| 指標 | 機関 | 最新値 | 基準日 |",
            "|------|------|-------|-------|",
        ]
        for ticker, info in result.get("current_rates", {}).items():
            lines.append(
                f"| {info['label']} | {info['bank']} "
                f"| **{info['value']:.3f}** | {info['date']} |"
            )

        new_ev = result.get("new_events", [])
        if new_ev:
            lines += ["", f"## ⚡ 新規検知イベント（{len(new_ev)}件）", ""]
            for ev in new_ev:
                chg_str = f"{ev['change_pt']:+.3f}%pt"
                lines.append(
                    f"### [{ev['bank']}] {ev['label']} {chg_str}"
                )
                lines.append(f"- **方向**: {ev['direction']}")
                lines.append(f"- **変化**: {ev['prev_value']:.3f} → {ev['curr_value']:.3f}")
                lines.append(f"- **検知日時**: {ev['detected_at']}")
                if ev.get("sector_impact"):
                    lines.append(f"- **業種影響**: {' / '.join(ev['sector_impact'])}")
                lines.append("")
        else:
            lines += ["", "## ⚡ 新規検知イベント", "", "今回の実行では有意な変化は検知されませんでした。", ""]

        all_ev = result.get("all_events", [])
        if all_ev:
            lines += ["## 全イベント履歴（直近10件）", "",
                      "| 検知日時 | 機関 | 指標 | 変化 | 方向 |",
                      "|---------|------|------|------|------|"]
            for ev in reversed(all_ev[-10:]):
                chg_str = f"{ev.get('change_pt', 0):+.3f}%pt"
                lines.append(
                    f"| {ev.get('detected_at','')[:10]} | {ev.get('bank','')} "
                    f"| {ev.get('label','')[:20]} | {chg_str} | {ev.get('direction','')[:20]} |"
                )

        out = macro_dir / f"policy_changes_{month}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"    💾 政策レポート保存: policy_changes_{month}.md")

    except Exception as e:
        print(f"    ⚠️  政策レポート保存失敗: {str(e)[:60]}")


# 単体テスト
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    result = run_policy_tracker(cfg)
    print(f"\n現在レート: {list(result.get('current_rates', {}).keys())}")
    print(f"新規イベント: {len(result.get('new_events', []))} 件")
    print(f"累積イベント: {len(result.get('all_events', []))} 件")
