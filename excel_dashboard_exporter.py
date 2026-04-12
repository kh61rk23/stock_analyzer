# ==============================================================
# excel_dashboard_exporter.py  v1.0  (Step D)
# 統合CSVエクスポーター（Excel Power Query 連携用）
# ==============================================================
# 【役割】
#   run_all_tasks() の results から日本株33業種 / 米国11セクター /
#   テーマETF のデータを統合し、Excel Power Query が自動取得できる
#   CSVを生成する。
#
# 【出力ファイル】
#   stock_data/data/dashboard/dashboard_latest.csv   ← Power Query はこれを参照
#   stock_data/data/dashboard/dashboard_YYYY-MM-DD.csv ← 履歴保持用
#
#   GitHub Actions の commit ステップでリポジトリに push されることで
#   raw.githubusercontent.com から Excel が自動取得できるようになる。
#
# 【CSV列定義（全カテゴリ共通スキーマ）】
#
#列番号  カラム名          型      説明
#──────────────────────────────────────────────────────────────
#  1     date             str     実行日（YYYY-MM-DD）
#  2     market           str     "JP" または "US"または"ETF"
#  3     category         str     業種コード（JP: "1001"〜"1033" / US: セクター名）
#  4     name_ja          str     業種・セクター日本語名
#  5     name_en          str     業種・セクター英語名
#  6     composite_score  float   総合スコア（0〜100）★現在 0.0 の問題あり
#  7     risk_level       str     "HIGH" / "MEDIUM" / "LOW" / "N/A"
#  8     risk_score       int     リスクスコア（数値化）
#  9     ret_1m           float   1ヶ月リターン（%）
# 10     ret_3m           float   3ヶ月リターン（%）
# 11     signal           str     tailwind / headwind / neutral / hot / warming / cooling
# 12     signal_score     int     シグナルスコア（正=追い風 / 負=逆風）
# 13     tailwind_count   int     追い風シグナル数
# 14     headwind_count   int     逆風シグナル数
# 15     etfs             str     関連ETFティッカー（カンマ区切り）
# 16     heat_score       float   テーマ熱量スコア（ETFテーマのみ）
# 17     arxiv_growth     str     ArXiv論文成長率（%、ETFテーマのみ）
# 18     top_company_1    str     代表銘柄1（例: "1332.T"）
# 19     top_company_2    str     代表銘柄2
# 20     notes            str     備考・メモ・補足（マクロシグナル要約など）
# 21     discovery_score  float   未発見銘柄スコア（0-100）[S-3追加]
# 22     accrual_tier     str     EDINETアクルーアルtier（AAA/AA/A/B/C/N/A）[S-2追加]
# 23     accrual_bonus    float   quality_scoreへの加算点
# 24     recession_adj    float   イールドカーブ補正量（recession_adjustment）[F-1追加]
# 25  signal_score      float  signal_aggregator 統合スコア（0-100）
# 26  market_regime     str    "リスクオン" / "中立" / "リスクオフ"
# 27  comps_bonus       float  同業種内比較ボーナス点
# 28  sr_bonus          float  サポレジ位置ボーナス点
# 29  inference_phase   str    推論エンジン市場フェーズ
# 30  pf_total_value_jpy  float  ポートフォリオ総時価（円）
# 31  pf_sharpe           float  現在PFシャープレシオ
# 32  pf_rebalance_count  int    リバランシング推奨銘柄数
#文字コード: UTF-8（BOMあり / Mac Excel対応）
# 【依存】config.py のみ（pandas / pathlib / csv）
# 【呼出】from excel_dashboard_exporter import run_excel_exporter
# ==============================================================

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: JP 業種名マスタ（コード → 英語名）
# ==============================================================
JP_SECTOR_EN = {
    "1001": "Fishery & Agriculture",   "1002": "Mining",
    "1003": "Construction",            "1004": "Foods",
    "1005": "Textiles & Apparel",      "1006": "Pulp & Paper",
    "1007": "Chemicals",               "1008": "Pharmaceutical",
    "1009": "Oil & Coal",              "1010": "Rubber",
    "1011": "Glass & Ceramics",        "1012": "Iron & Steel",
    "1013": "Nonferrous Metals",       "1014": "Metal Products",
    "1015": "Machinery",               "1016": "Electric Appliances",
    "1017": "Transportation Equipment","1018": "Precision Instruments",
    "1019": "Other Products",          "1020": "Electric Power & Gas",
    "1021": "Land Transportation",     "1022": "Marine Transportation",
    "1023": "Air Transportation",      "1024": "Warehousing",
    "1025": "Information & Communication","1026": "Wholesale Trade",
    "1027": "Retail Trade",            "1028": "Banks",
    "1029": "Securities",              "1030": "Insurance",
    "1031": "Other Finance",           "1032": "Real Estate",
    "1033": "Services",
}

# risk_level → 数値（Power BIでソート・色分けに使用）
RISK_SCORE = {"low": 1, "medium": 2, "high": 3}

# signal → 数値（棒グラフ・条件付き書式に使用）
SIGNAL_SCORE = {
    "tailwind": 2, "warming": 1, "neutral": 0,
    "cooling": -1, "headwind": -2,
}

# ==============================================================
# BLOCK 1: JPセクターデータ → CSV行
# ==============================================================

def _rows_from_jp_sectors(sector_results: dict, date_str: str,
                           screener_result: Optional[dict]) -> list:
    """
    results["sectors"] から CSV 行リストを生成する。

    screener_result が存在する場合は各業種の通過銘柄上位2件を付与する。
    """
    # スクリーナー結果をセクターコードでインデックス化
    top_cand_by_sector: dict = {}
    if screener_result and isinstance(screener_result, dict):
        for c in screener_result.get("candidates", []):
            if not c.get("pass_filters"):
                continue
            code = c.get("sector_code", "")
            top_cand_by_sector.setdefault(code, [])
            top_cand_by_sector[code].append(c)

    rows = []
    for code, data in sector_results.items():
        if not isinstance(data, dict):
            continue

        score      = data.get("composite_score", 0)
        risk       = data.get("risk_level", "")
        name_ja    = data.get("sector_name_ja", code)
        name_en    = JP_SECTOR_EN.get(code, "")

        # マクロシグナル要約
        macro_sigs = data.get("macro_signals", [])
        tw = sum(1 for s in macro_sigs if isinstance(s, dict) and s.get("signal") == "tailwind")
        hw = sum(1 for s in macro_sigs if isinstance(s, dict) and s.get("signal") == "headwind")
        notes = f"追い風{tw} 逆風{hw}" if (tw or hw) else ""

        # シグナル（スコアから簡易判定）
        if score >= 65:
            signal = "tailwind"
        elif score <= 40:
            signal = "headwind"
        else:
            signal = "neutral"

        tops_data = top_cand_by_sector.get(code, [])
        c1 = tops_data[0] if len(tops_data) > 0 else {}
        c2 = tops_data[1] if len(tops_data) > 1 else {}
        
        top1 = c1.get("ticker", "")
        top2 = c2.get("ticker", "")

        def _get_val(d, k):
            return round(float(d[k]), 2) if d.get(k) is not None and d.get(k) != "" else ""

        rows.append({
            "date":            date_str,
            "market":          "JP",
            "category":        code,
            "name_ja":         name_ja,
            "name_en":         name_en,
            "composite_score": round(float(score), 1) if score else 0.0,
            "risk_level":      risk,
            "risk_score":      RISK_SCORE.get(risk, 0),
            "ret_1m":          "",
            "ret_3m":          "",
            "signal":          signal,
            "signal_score":    SIGNAL_SCORE.get(signal, 0),
            "tailwind_count":  tw,
            "headwind_count":  hw,
            "etfs":            "",
            "heat_score":      round(float(score), 1) if score else 0.0,
            "arxiv_growth":    "",
            "top_company_1":   top1,
            "top_company_2":   top2,
            "notes":           notes,
            "discovery_score": _get_val(c1, "discovery_score"),
            "accrual_tier":    c1.get("accrual_tier", ""),
            "accrual_bonus":   _get_val(c1, "accrual_bonus"),
            "recession_adj":   _get_val(data, "recession_adjustment"),
            "comps_bonus":     _get_val(c1, "comps_bonus"),
            "sr_bonus":        _get_val(c1, "sr_bonus"),
            "total_signal_score": "",
            "market_regime": "",
            "inference_phase": "",
            "pf_total_value_jpy": "",
            "pf_sharpe": "",
            "pf_rebalance_count": "",
        })

    return rows


# ==============================================================
# BLOCK 2: USセクターデータ → CSV行
# ==============================================================

def _rows_from_us_sectors(us_result: dict, date_str: str) -> list:
    """
    results["us_sectors"] から CSV 行リストを生成する。

    us_sector_pipeline.py の出力形式:
      {"sector_results": [{"sector": {...}, "score_result": {...}}, ...]}
    """
    rows = []
    sector_list = us_result.get("sector_results", [])

    for item in sector_list:
        if not isinstance(item, dict):
            continue
        sec = item.get("sector", {})
        sr  = item.get("score_result", {})
        etf = sr.get("etf_data", {})

        def _f(v):
            try:
                return round(float(v), 2) if v is not None else ""
            except (TypeError, ValueError):
                return ""

        rows.append({
            "date":            date_str,
            "market":          "US",
            "category":        sec.get("etf", ""),
            "name_ja":         sec.get("name_ja", ""),
            "name_en":         sec.get("name_en", ""),
            "composite_score": round(float(sr.get("score", 0)), 1),
            "risk_level":      sr.get("risk_level", ""),
            "risk_score":      RISK_SCORE.get(sr.get("risk_level", ""), 0),
            "signal_score":    SIGNAL_SCORE.get(signal, 0),
            "tailwind_count":  "",
            "headwind_count":  "",
            "ret_1m":          _f(etf.get("ret_1m")),
            "ret_3m":          _f(etf.get("ret_3m")),
            "signal":          "tailwind" if sr.get("score", 0) >= 65
                               else ("headwind" if sr.get("score", 0) <= 40
                                     else "neutral"),
            "etfs":            sec.get("etf", ""),
            "heat_score":      "",
            "arxiv_growth":    "",
            "top_company_1":   "",
            "top_company_2":   "",
            "notes":           " | ".join(sr.get("signals", []))[:80],
            "discovery_score": "",
            "accrual_tier":    "",
            "accrual_bonus":   "",
            "recession_adj":   "",
            "comps_bonus":     "",
            "sr_bonus":        "",
            "total_signal_score": "",
            "market_regime": "",
            "inference_phase": "",
            "pf_total_value_jpy": "",
            "pf_sharpe": "",
            "pf_rebalance_count": "",
        })
    
    return rows


# ==============================================================
# BLOCK 3: テーマETFデータ → CSV行
# ==============================================================

def _rows_from_etf_tracker(etf_result: dict, date_str: str) -> list:
    """
    results["etf_tracker"] から CSV 行リストを生成する。
    """
    rows = []
    for theme in etf_result.get("themes", []):
        if not isinstance(theme, dict):
            continue

        ad           = theme.get("arxiv_detail", {})
        arxiv_growth = ad.get("growth_rate", "")

        # ETF 詳細から1M/3Mリターンの平均を算出
        details = theme.get("etf_details", [])
        if details:
            ret1m_vals = [d.get("ret_1m") for d in details if d.get("ret_1m") is not None]
            ret3m_vals = [d.get("ret_3m") for d in details if d.get("ret_3m") is not None]
            ret_1m = round(sum(ret1m_vals) / len(ret1m_vals), 2) if ret1m_vals else ""
            ret_3m = round(sum(ret3m_vals) / len(ret3m_vals), 2) if ret3m_vals else ""
        else:
            ret_1m = ret_3m = ""

        signal = theme.get("signal", "neutral")   # hot/warming/neutral/cooling

        rows.append({
            "date":            date_str,
            "market":          "ETF",
            "category":        theme.get("theme_id", ""),
            "name_ja":         theme.get("theme_name", ""),
            "name_en":         theme.get("theme_name_en", ""),
            "composite_score": theme.get("etf_momentum_score", ""),
            "risk_level":      "high" if signal == "hot"
                               else ("medium" if signal == "warming" else "low"),
            "ret_1m":          ret_1m,
            "ret_3m":          ret_3m,
            "signal":          signal,
            "etfs":            ",".join(theme.get("etfs", [])),
            "heat_score":      theme.get("heat_score", ""),
            "arxiv_growth":    round(float(arxiv_growth), 1)
                               if (arxiv_growth != "" and arxiv_growth is not None) else "",
            "top_company_1":   "",
            "top_company_2":   "",
            "notes":           (f"ArXiv直近4週:{ad.get('recent_4w',0)}件"
                                if ad.get("recent_4w") else ""),
            "discovery_score": "",
            "accrual_tier":    "",
            "accrual_bonus":   "",
            "recession_adj":   "",
            "comps_bonus":     "",
            "sr_bonus":        "",
            "total_signal_score": "",
            "market_regime": "",
            "inference_phase": "",
            "pf_total_value_jpy": "",
            "pf_sharpe": "",
            "pf_rebalance_count": "",
        })

    return rows


# ==============================================================
# BLOCK 4: CSV書き出し
# ==============================================================
FIELDNAMES = [
    "date", "market", "category", "name_ja", "name_en",
    "composite_score",
    "risk_level",      "risk_score",       # risk_score: low=1 medium=2 high=3
    "ret_1m",          "ret_3m",
    "signal",          "signal_score",     # signal_score: tailwind=2 ~ headwind=-2
    "tailwind_count",  "headwind_count",   # 追い風・逆風件数（整数）
    "etfs",            "heat_score",       "arxiv_growth",
    "top_company_1",   "top_company_2",
    "notes",
    "discovery_score", "accrual_tier", "accrual_bonus", "recession_adj", 
    "total_signal_score", "market_regime", "comps_bonus", "sr_bonus", 
    "inference_phase", "pf_total_value_jpy", "pf_sharpe", "pf_rebalance_count"
]

def _write_csv(rows: list, path: Path) -> None:
    """BOM付きUTF-8（Excel直接開き対応）でCSVを書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

def _upload_to_gdrive(latest_csv: Path, dated_csv: Path,
                      file_id: str, cfg) -> None:
    """
    dashboard_latest.csv を Google Drive の固定ファイルIDで上書きアップロードし、
    dashboard_YYYY-MM-DD.csv を同フォルダに新規アップロードする（履歴保持）。
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token         = None,
        refresh_token = getattr(cfg, "GOOGLE_OAUTH_REFRESH_TOKEN", ""),
        client_id     = getattr(cfg, "GOOGLE_OAUTH_CLIENT_ID", ""),
        client_secret = getattr(cfg, "GOOGLE_OAUTH_CLIENT_SECRET", ""),
        token_uri     = "https://oauth2.googleapis.com/token",
    )
    service = build("drive", "v3", credentials=creds)

    # ① dashboard_latest.csv を固定IDで上書き
    media = MediaFileUpload(str(latest_csv), mimetype="text/csv", resumable=False)
    service.files().update(fileId=file_id, media_body=media).execute()
    print(f"  ☁️  Drive 上書き完了: dashboard_latest.csv (id={file_id[:8]}...)")

    # ② dashboard_YYYY-MM-DD.csv を同フォルダに新規アップロード（履歴）
    file_meta = service.files().get(fileId=file_id, fields="parents").execute()
    parents   = file_meta.get("parents", [])
    media2    = MediaFileUpload(str(dated_csv), mimetype="text/csv")
    service.files().create(
        body       = {"name": dated_csv.name, "parents": parents},
        media_body = media2,
    ).execute()
    print(f"  ☁️  Drive 新規保存: {dated_csv.name}")

# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_excel_exporter(cfg, results: dict) -> dict:
    """
    run_all_tasks() の results を受け取り統合CSVを出力する。

    Args:
        cfg     : config モジュール
        results : run_all_tasks() の返り値

    Returns:
        {
          "csv_latest_path": str,   # dashboard_latest.csv のパス
          "csv_dated_path":  str,   # dashboard_YYYY-MM-DD.csv のパス
          "row_count":       int,   # 書き出した行数
          "breakdown":       dict,  # {JP: int, US: int, ETF: int}
        }
    """
    print("\n  📊 Excelダッシュボード CSV エクスポート開始...")

    today_str    = datetime.now().strftime("%Y-%m-%d")
    dashboard_dir = Path(getattr(cfg, "DATA_DIR", "data")) / "dashboard"

    all_rows = []

    # ── JP 33業種 ──────────────────────────────────────────
    jp_sectors = results.get("sectors", {})
    if jp_sectors:
        screener = results.get("screener")
        jp_rows  = _rows_from_jp_sectors(jp_sectors, today_str, screener)
        all_rows.extend(jp_rows)
        print(f"    JP 業種: {len(jp_rows)}行")
    else:
        print("    JP 業種: データなし")
        jp_rows = []

    # ── US 11セクター ─────────────────────────────────────
    us_result = results.get("us_sectors", {})
    if us_result and isinstance(us_result, dict):
        us_rows = _rows_from_us_sectors(us_result, today_str)
        all_rows.extend(us_rows)
        print(f"    US セクター: {len(us_rows)}行")
    else:
        print("    US セクター: データなし")
        us_rows = []

    # ── テーマETF ─────────────────────────────────────────
    etf_result = results.get("etf_tracker", {})
    if etf_result and isinstance(etf_result, dict):
        etf_rows = _rows_from_etf_tracker(etf_result, today_str)
        all_rows.extend(etf_rows)
        print(f"    テーマETF: {len(etf_rows)}行")
    else:
        print("    テーマETF: データなし")
        etf_rows = []

    if not all_rows:
        print("  ⚠️  全データが空のためCSV出力をスキップ")
        return {
            "csv_latest_path": "", "csv_dated_path": "",
            "row_count": 0,
            "breakdown": {"JP": 0, "US": 0, "ETF": 0},
        }

    # ── 全体指標の抽出と適用（設計書 No.25〜32） ────────────────────────
    # ※ 設計書No.25の signal_score は既存12番と重複するため total_signal_score に変更
    total_sig_score = results.get("total_signal_score", {}).get("total_signal_score", "")
    if total_sig_score == "":
        total_sig_score = results.get("signal_aggregator", {}).get("total_score", "")
    
    market_regime = results.get("signal_aggregator", {}).get("market_regime", "")
    inference_phase = results.get("inference", {}).get("phase_label", "")
    
    pf_opt = results.get("portfolio_optimizer", {})
    pf_total_val = pf_opt.get("current", {}).get("total_value_jpy", "")
    pf_sharpe = pf_opt.get("current", {}).get("sharpe_ratio", "")
    rebal_list = pf_opt.get("rebalance", [])
    pf_rebal_count = sum(1 for r in rebal_list if r.get("action") != "HOLD") if rebal_list else ""

    for r in all_rows:
        r["total_signal_score"] = round(float(total_sig_score), 1) if total_sig_score != "" else ""
        r["market_regime"]      = market_regime
        r["inference_phase"]    = inference_phase
        r["pf_total_value_jpy"] = pf_total_val
        r["pf_sharpe"]          = round(float(pf_sharpe), 2) if pf_sharpe != "" else ""
        r["pf_rebalance_count"] = pf_rebal_count

    # composite_score 降順でソート（market別に）
    all_rows.sort(key=lambda r: (
        {"JP": 0, "US": 1, "ETF": 2}.get(r["market"], 9),
        -(float(r["composite_score"]) if (r["composite_score"] not in ("", None)) else 0),
    ))

    # 書き出し
    latest_path = dashboard_dir / "dashboard_latest.csv"
    dated_path  = dashboard_dir / f"dashboard_{today_str}.csv"

    _write_csv(all_rows, latest_path)
    _write_csv(all_rows, dated_path)

    print(
        f"  ✅ CSV出力完了: {len(all_rows)}行 "
        f"(JP:{len(jp_rows)} US:{len(us_rows)} ETF:{len(etf_rows)})"
    )
    print(f"     → {latest_path}")

    # ── Google Drive アップロード（履歴保存）──────────────────
    gdrive_file_id = getattr(cfg, "GDRIVE_DASHBOARD_FILE_ID", "")
    if gdrive_file_id:
        try:
            _upload_to_gdrive(latest_path, dated_path, gdrive_file_id, cfg)
        except Exception as e:
            print(f"  ⚠️  Drive アップロード失敗: {str(e)[:60]}")
    else:
        print("  ⏩ Drive アップロード: スキップ（GDRIVE_DASHBOARD_FILE_ID 未設定）")
    
    return {
        "csv_latest_path": str(latest_path),
        "csv_dated_path":  str(dated_path),
        "row_count":       len(all_rows),
        "breakdown": {
            "JP":  len(jp_rows),
            "US":  len(us_rows),
            "ETF": len(etf_rows),
        },
    }


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py",
              str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break

    mock_results = {
        "sectors": {
            "1016": {"composite_score": 78.0, "sector_name_ja": "電気機器",
                     "risk_level": "medium", "macro_signals": []},
            "1028": {"composite_score": 55.0, "sector_name_ja": "銀行業",
                     "risk_level": "low", "macro_signals": []},
        },
        "us_sectors": {
            "sector_results": [
                {"sector": {"etf": "XLK", "name_en": "Technology",
                            "name_ja": "テクノロジー"},
                 "score_result": {"score": 72.0, "risk_level": "medium",
                                  "signals": [], "etf_data": {"ret_1m": 3.2,
                                                               "ret_3m": 8.1}}},
            ]
        },
    }
    r = run_excel_exporter(cfg, mock_results)
    print(f"出力行数: {r['row_count']}")
    print(f"パス: {r['csv_latest_path']}")
