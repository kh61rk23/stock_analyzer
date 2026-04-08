# ==============================================================
# us_sector_bridge_enhanced.py  v1.0  (Phase US-5)
# SIC/CIK マッピング + JP業種連携強化
# ==============================================================
# 【役割】
#   jp_us_sector_bridge.py の拡張版。
#   CIK → ticker → SIC code → JP業種コード の4段チェーンで
#   「米国半導体企業の決算サプライズ → JP電気機器に波及シグナル」を実現する。
#
# 【従来の jp_us_sector_bridge との違い】
#   従来: ETFスコアの相関（週次・遅延あり）
#   本版: 8-K決算発表（当日）→ SICマップ → JP業種に即日シグナル
#
# 【SIC マッピング設計】
#   SIC code（米国標準産業分類）→ JP東証業種コードの対応を定義。
#   例: SIC 3674（半導体）→ JP 1016（電気機器）
#       SIC 7372（ソフトウェア）→ JP 1025（情報通信業）
#
# 【処理フロー】
#   1. us_8k_monitor の events から earnings_tickers を受け取る
#   2. CIK → SIC を SEC submissions API から取得
#   3. SIC → JP業種コードに変換
#   4. 決算の強弱（beat/miss）を us_fundamental_rotator の EPS データで判定
#   5. CROSS_SECTOR_RULES 形式のシグナルとして results に追加
#
# 【us_sector_mapping.json の構造】
#   {
#     "ticker": {
#       "cik":       int,
#       "sic":       int,
#       "sic_desc":  str,
#       "jp_sectors": ["1016", ...],
#       "last_updated": str,
#     }, ...
#   }
#
# 【依存】requests / config.py / us_8k_monitor / us_fundamental_rotator
# ==============================================================

from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: SIC → JP業種コード マッピング定義
# ==============================================================

# SIC 主要グループ → JP 東証業種コード
# SIC の2桁グループコードで判定（詳細4桁は別テーブル）
SIC_TO_JP_SECTORS: dict[int | str, list[str]] = {
    # ── 情報技術 ─────────────────────────────────────────────
    3674: ["1016"],          # 半導体・半導体製造装置 → 電気機器
    3672: ["1016"],          # プリント回路基板
    3678: ["1016"],          # 電子コネクタ
    3669: ["1016"],          # 通信機器
    3661: ["1025"],          # 固定電話システム → 情報通信業
    3663: ["1025"],          # 無線通信機器
    7372: ["1025"],          # パッケージソフトウェア
    7371: ["1025"],          # コンピュータサービス
    7374: ["1025"],          # データ処理サービス
    3577: ["1016", "1025"],  # コンピュータ周辺機器
    3571: ["1016"],          # 電子計算機
    3579: ["1016"],          # 事務機械
    # ── 自動車・輸送機器 ──────────────────────────────────────
    3711: ["1017"],          # 自動車・トラック → 輸送用機器
    3714: ["1017"],          # 自動車部品
    3716: ["1017"],          # 自動車・バス車体
    3724: ["1017"],          # 航空機エンジン
    3728: ["1017"],          # 航空機部品
    3812: ["1017", "1015"],  # 防衛電子・通信機器 → 輸送用+機械
    # ── 機械・産業機械 ────────────────────────────────────────
    3559: ["1015"],          # 特殊産業機械 → 機械
    3560: ["1015"],          # 一般産業機械
    3562: ["1015"],          # ボールベアリング・ローラーベアリング
    3569: ["1015"],          # 産業機械一般
    3537: ["1015"],          # 産業用トラック・トレーラー
    3585: ["1015"],          # 冷凍・冷暖房機器
    # ── 精密機器・医療機器 ────────────────────────────────────
    3841: ["1018"],          # 外科・医療機器 → 精密機器
    3826: ["1018"],          # 実験分析機器
    3827: ["1018"],          # 光学機器
    3825: ["1018"],          # 測定・制御機器
    # ── 医薬品・バイオ ────────────────────────────────────────
    2836: ["1008"],          # 医薬品製剤 → 医薬品
    2833: ["1008"],          # 製薬原料
    2835: ["1008"],          # 診断用試薬
    8731: ["1008"],          # 商業医薬研究
    # ── 化学 ──────────────────────────────────────────────────
    2819: ["1007"],          # 化学工業品 → 化学
    2821: ["1007"],          # プラスチック材料
    2860: ["1007"],          # 工業薬品
    2911: ["1009"],          # 石油精製 → 石油・石炭製品
    1311: ["1009"],          # 原油・天然ガス採掘
    # ── 鉄鋼・非鉄 ───────────────────────────────────────────
    3312: ["1012"],          # 鉄鋼製造 → 鉄鋼
    3316: ["1012"],          # 冷間圧延鋼板
    3334: ["1013"],          # 一次アルミニウム精錬 → 非鉄金属
    3350: ["1013"],          # ローリングされた非鉄金属
    # ── 金融 ──────────────────────────────────────────────────
    6022: ["1028"],          # 州認定商業銀行 → 銀行業
    6020: ["1028"],          # 連邦認定商業銀行
    6159: ["1031"],          # 連邦保証住宅ローン → その他金融
    6282: ["1029"],          # 投資顧問 → 証券・商品先物
    6211: ["1029"],          # 証券ディーラー
    6311: ["1030"],          # 生命保険 → 保険業
    6321: ["1030"],          # 損害保険
    # ── 小売・サービス ────────────────────────────────────────
    5945: ["1027"],          # 趣味・玩具店 → 小売業
    5411: ["1027"],          # 食料品スーパー
    5651: ["1027"],          # アパレル専門店
    7011: ["1033"],          # ホテル → サービス業
    7812: ["1033"],          # 映画制作・配給
    7990: ["1033"],          # エンターテインメント
    # ── 不動産 ────────────────────────────────────────────────
    6552: ["1032"],          # 不動産デベロッパー → 不動産業
    6510: ["1032"],          # 不動産管理
    # ── 電力・ガス ────────────────────────────────────────────
    4911: ["1020"],          # 電力 → 電気・ガス業
    4922: ["1020"],          # 天然ガス配給
    # ── 陸運・倉庫 ───────────────────────────────────────────
    4213: ["1021"],          # トラック輸送 → 陸運業
    4412: ["1022"],          # 外航海運 → 海運業
    4512: ["1023"],          # 航空運送 → 空運業
    4731: ["1024"],          # 貨物輸送仲介 → 倉庫・運輸関連
}

# SIC 2桁グループ → JP業種（詳細マッピングがない場合のフォールバック）
SIC_2DIGIT_TO_JP: dict[str, list[str]] = {
    "36": ["1016"],   # 電子・電気機器
    "35": ["1015"],   # 産業機械
    "37": ["1017"],   # 輸送機器
    "38": ["1018"],   # 測定機器・光学
    "28": ["1007"],   # 化学
    "29": ["1009"],   # 石油
    "33": ["1012"],   # 一次金属（鉄鋼寄り）
    "73": ["1025"],   # コンピュータ関連サービス
    "62": ["1029"],   # 証券・投資
    "60": ["1028"],   # 銀行
    "63": ["1030"],   # 保険
    "49": ["1020"],   # 電力・ガス・水道
    "79": ["1033"],   # 娯楽・サービス
    "54": ["1027"],   # 食料品小売
}

# SEC API
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{:010d}.json"
REQUEST_DELAY   = 0.12
REQUEST_TIMEOUT = 10

# マッピングキャッシュファイル
MAPPING_FILE    = "us_sector_mapping.json"
MAPPING_CACHE_DAYS = 30   # SIC情報は30日キャッシュ


# ==============================================================
# BLOCK 1: ファイル I/O
# ==============================================================

def _data_dir(cfg) -> Path:
    return Path(getattr(cfg, "DATA_DIR", "stock_data/data"))


def load_sector_mapping(cfg) -> dict:
    """us_sector_mapping.json を読み込む。"""
    p = _data_dir(cfg) / MAPPING_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_sector_mapping(cfg, mapping: dict) -> None:
    p = _data_dir(cfg) / MAPPING_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    ⚠️  us_sector_mapping.json 保存失敗: {e}")


# ==============================================================
# BLOCK 2: SIC 取得（SEC submissions API）
# ==============================================================

def _fetch_sic_from_submissions(cik: int) -> Optional[dict]:
    """
    SEC submissions API から SIC コードを取得する。

    Returns:
        {"sic": int, "sic_desc": str, "name": str} | None
    """
    try:
        import requests
        url  = SUBMISSIONS_URL.format(cik)
        resp = requests.get(
            url,
            headers={"User-Agent": "stock_analyzer/1.0 (educational use)"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        sic      = data.get("sic")
        sic_desc = data.get("sicDescription", "")
        name     = data.get("name", "")
        if sic:
            return {"sic": int(sic), "sic_desc": sic_desc, "name": name}
    except Exception:
        pass
    return None


def _sic_to_jp_sectors(sic: int) -> list[str]:
    """
    SIC コードから JP業種コードリストに変換する。

    優先: 4桁完全一致 → 2桁グループ一致 → []
    """
    # 4桁完全一致
    exact = SIC_TO_JP_SECTORS.get(sic)
    if exact:
        return exact

    # 2桁グループ一致
    sic_2d = str(sic)[:2]
    group  = SIC_2DIGIT_TO_JP.get(sic_2d)
    if group:
        return group

    return []


def update_sector_mapping(cfg, tickers_with_cik: list[dict]) -> dict:
    """
    ticker + CIK のリストから SIC → JP業種のマッピングを更新する。

    Args:
        tickers_with_cik: [{"ticker": str, "cik": int}, ...]

    Returns:
        更新後のマッピング dict
    """
    mapping = load_sector_mapping(cfg)
    today   = date.today().strftime("%Y-%m-%d")
    updated = 0

    for item in tickers_with_cik:
        ticker = item.get("ticker", "").upper()
        cik    = item.get("cik", 0)
        if not ticker or not cik:
            continue

        # キャッシュ有効チェック
        existing = mapping.get(ticker, {})
        last_upd = existing.get("last_updated", "")
        if last_upd:
            try:
                days_old = (date.today() - date.fromisoformat(last_upd)).days
                if days_old < MAPPING_CACHE_DAYS and existing.get("sic"):
                    continue   # キャッシュ有効
            except Exception:
                pass

        # SEC から SIC 取得
        sic_data = _fetch_sic_from_submissions(cik)
        time.sleep(REQUEST_DELAY)

        if not sic_data:
            continue

        jp_sectors = _sic_to_jp_sectors(sic_data["sic"])
        mapping[ticker] = {
            "cik":          cik,
            "sic":          sic_data["sic"],
            "sic_desc":     sic_data["sic_desc"],
            "company_name": sic_data["name"],
            "jp_sectors":   jp_sectors,
            "last_updated": today,
        }
        updated += 1

    if updated:
        save_sector_mapping(cfg, mapping)
        print(f"    📊 US sector mapping 更新: {updated}社")

    return mapping


# ==============================================================
# BLOCK 3: 8-K 決算シグナル生成
# ==============================================================

def _eval_earnings_strength(ticker: str, cfg) -> str:
    """
    us_fundamental_rotator の EPS データから
    最新決算の強弱（beat/miss/neutral）を判定する。

    Returns:
        "beat" | "miss" | "neutral" | "unknown"
    """
    try:
        fund_path = _data_dir(cfg) / "us_fundamentals_db" / f"{ticker.replace('/', '_')}.json"
        if not fund_path.exists():
            return "unknown"
        data = json.loads(fund_path.read_text(encoding="utf-8"))
        eps_history = data.get("eps_history", [])
        if len(eps_history) < 2:
            return "unknown"

        latest  = eps_history[0].get("val", 0) or 0
        prev_yy = eps_history[4].get("val", 0) if len(eps_history) > 4 else None

        if prev_yy is None or abs(prev_yy) < 0.001:
            return "neutral"

        yoy_growth = (latest - prev_yy) / abs(prev_yy)
        if yoy_growth >= 0.10:   # YoY +10%超 = beat
            return "beat"
        elif yoy_growth <= -0.10:  # YoY -10%以下 = miss
            return "miss"
        return "neutral"

    except Exception:
        return "unknown"


def generate_8k_cross_sector_signals(
    earnings_tickers: list[str],
    sector_mapping:   dict,
    cfg,
) -> list[dict]:
    """
    8-K 決算発表 × SIC マッピングから JP業種への CROSS_SECTOR シグナルを生成する。

    処理:
      1. 決算銘柄の SIC → JP業種変換
      2. EPS beat/miss を判定
      3. JP業種への tailwind/headwind シグナルを生成

    Returns:
        CROSS_SECTOR_RULES 形式のシグナルリスト
    """
    signals = []
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})

    for ticker in earnings_tickers:
        mapping_entry = sector_mapping.get(ticker.upper())
        if not mapping_entry:
            continue

        jp_sectors = mapping_entry.get("jp_sectors", [])
        if not jp_sectors:
            continue

        sic_desc  = mapping_entry.get("sic_desc", "")
        company   = mapping_entry.get("company_name", ticker)
        strength  = _eval_earnings_strength(ticker, cfg)

        if strength == "unknown":
            continue

        if strength == "beat":
            direction = "tailwind"
            pts       = 3.0
            label     = f"US決算beat: {company}({sic_desc}) → JP{jp_sectors}"
        elif strength == "miss":
            direction = "headwind"
            pts       = -3.0
            label     = f"US決算miss: {company}({sic_desc}) → JP{jp_sectors}"
        else:
            continue

        signals.append({
            "rule_id":          f"US_8K_{ticker}_{direction.upper()}",
            "label":            label,
            "source":           "us_8k_sic_bridge",
            "trigger_ticker":   ticker,
            "sic":              mapping_entry.get("sic"),
            "sic_desc":         sic_desc,
            "sectors_tailwind": jp_sectors if direction == "tailwind" else [],
            "sectors_headwind": jp_sectors if direction == "headwind" else [],
            "pts_adj":          pts,
            "signal":           direction,
            "filing_date":      date.today().strftime("%Y-%m-%d"),
        })

    return signals


# ==============================================================
# BLOCK 4: ISM製造業 × JP輸出業種シグナル強化
# ==============================================================

def enhance_ism_cross_sector_signal(
    results:        dict,
    sector_mapping: dict,
    cfg = None,
) -> list[dict]:
    """
    CROSS_SECTOR_RULES の "us_ism_manufacturing_up" を
    US企業の実際の受注データ（8-K / XBRL）で補強する。

    us_ism_manufacturing_up は macro_snapshot の us_ism 値で発動するが、
    実際の米国IT・半導体企業の業績 beat が重なる場合は信頼度を上げる。

    Returns:
        補強シグナルのリスト（既存 CROSS_SECTOR シグナルに追加する）
    """
    us_macro = results.get("us_macro", {})
    us_8k    = results.get("us_8k", {})

    signals = []
    if not us_macro or not us_8k:
        return signals

    # ISM が拡大局面（>52）かつ 8-K 決算beat あり
    macro_snap = results.get("macro_snapshot", {})
    us_ism     = macro_snap.get("us_ism")
    if not us_ism or us_ism < 52.0:
        return signals

    earnings_tickers = us_8k.get("earnings_tickers", [])
    if not earnings_tickers:
        return signals

    # 半導体・IT業種の beat が重なったら JP輸出業種に追加 +2pt
    it_beats = []
    for ticker in earnings_tickers:
        entry = sector_mapping.get(ticker.upper(), {})
        jp_s  = entry.get("jp_sectors", [])
        sic   = entry.get("sic", 0)
        # SIC 3674（半導体）or 7372（ソフト）の beat
        if sic in (3674, 3672, 7372, 7371) or "1016" in jp_s or "1025" in jp_s:
            strength = _eval_earnings_strength(ticker, cfg)
            if strength == "beat":
                it_beats.append(ticker)

    if it_beats:
        signals.append({
            "rule_id":          "US_ISM_IT_BEAT_ENHANCE",
            "label":            f"ISM拡大×US IT/半導体beat（{it_beats[:3]}）→ JP輸出強化",
            "source":           "us_sector_bridge_enhanced",
            "sectors_tailwind": ["1016", "1015", "1018", "1017"],
            "sectors_headwind": [],
            "pts_adj":          2.0,
            "signal":           "tailwind",
        })
        print(f"    🔗 [US-5] ISM×IT beat強化シグナル: {it_beats[:3]}")

    return signals


# ==============================================================
# BLOCK 5: メインエントリポイント
# ==============================================================

def run_us_sector_bridge_enhanced(cfg, results: dict = None) -> dict:
    """
    US-5 SIC/CIK マッピング + JP業種連携強化を実行する。

    stock_analyzer_main.py の jp_us_bridge ブロックの直後に呼び出す:
        _usbe = _load_local_module("us_sector_bridge_enhanced", ...)
        usbe_result = _usbe.run_us_sector_bridge_enhanced(cfg, results)
        results["us_sector_bridge"] = usbe_result
        # CROSS_SECTOR シグナルに追加
        if usbe_result.get("cross_signals"):
            existing = results.get("cross_sector_signals", [])
            results["cross_sector_signals"] = existing + usbe_result["cross_signals"]

    処理:
      1. us_8k_monitor の earnings_tickers を取得
      2. CIK → SIC マッピングを更新（30日キャッシュ）
      3. SIC → JP業種変換
      4. EPS beat/miss 判定
      5. JP業種への CROSS_SECTOR シグナルを生成
      6. ISM × IT beat の複合シグナルを生成

    Args:
        cfg     : config
        results : run_all_tasks() の results dict

    Returns:
        {
          "cross_signals":   [signal_dict, ...],
          "mapping_updated": int,
          "jp_affected":     [sector_code, ...],
          "beat_tickers":    [ticker, ...],
          "miss_tickers":    [ticker, ...],
          "timestamp":       str,
        }
    """
    if not getattr(cfg, "ENABLE_US_SECTOR_BRIDGE", True):
        return {"skipped": True}

    print("\n  🌐 US-JP セクターブリッジ強化版 開始...")
    results = results or {}
    t0      = datetime.now()

    # ── Step1: us_8k の決算銘柄と CIK マップを取得 ─────────
    us_8k = results.get("us_8k", {})
    earnings_tickers = us_8k.get("earnings_tickers", [])
    screener_hits    = us_8k.get("screener_hits", [])
    # 決算 + screener対象を対象に
    target_tickers   = list(dict.fromkeys(earnings_tickers + screener_hits))

    if not target_tickers:
        print("    ⏩ 本日の8-K決算なし / screener対象なし → スキップ")
        return {
            "cross_signals":   [],
            "mapping_updated": 0,
            "jp_affected":     [],
            "timestamp":       t0.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── Step2: CIK マップ読み込み ────────────────────────────
    cik_map_path = _data_dir(cfg) / "us_cik_map.json"
    cik_map      = {}
    if cik_map_path.exists():
        try:
            ticker_to_cik = json.loads(cik_map_path.read_text(encoding="utf-8"))
            cik_map       = {k: v for k, v in ticker_to_cik.items() if isinstance(v, int)}
        except Exception:
            pass

    tickers_with_cik = [
        {"ticker": t, "cik": cik_map.get(t.upper(), 0)}
        for t in target_tickers
        if cik_map.get(t.upper(), 0) > 0
    ]

    # ── Step3: SIC マッピング更新 ────────────────────────────
    sector_mapping = update_sector_mapping(cfg, tickers_with_cik)

    # ── Step4: 8-K × SIC × EPS → JP業種シグナル生成 ─────────
    cross_signals = generate_8k_cross_sector_signals(
        earnings_tickers, sector_mapping, cfg
    )

    # ── Step5: ISM × IT beat 複合シグナル ───────────────────
    ism_signals = enhance_ism_cross_sector_signal(results, sector_mapping, cfg)
    cross_signals.extend(ism_signals)

    # 集計
    beat_tickers = [
        s["trigger_ticker"] for s in cross_signals
        if s.get("signal") == "tailwind" and s.get("trigger_ticker")
    ]
    miss_tickers = [
        s["trigger_ticker"] for s in cross_signals
        if s.get("signal") == "headwind" and s.get("trigger_ticker")
    ]
    jp_affected  = list(dict.fromkeys(
        code for s in cross_signals
        for code in (s.get("sectors_tailwind", []) + s.get("sectors_headwind", []))
    ))

    elapsed = (datetime.now() - t0).seconds
    print(
        f"    ✅ セクターブリッジ完了（{elapsed}秒）: "
        f"シグナル{len(cross_signals)}件 / "
        f"JP影響業種{len(jp_affected)}業種 / "
        f"beat{len(beat_tickers)} / miss{len(miss_tickers)}"
    )

    return {
        "cross_signals":   cross_signals,
        "mapping_updated": len(tickers_with_cik),
        "jp_affected":     jp_affected,
        "beat_tickers":    beat_tickers,
        "miss_tickers":    miss_tickers,
        "timestamp":       t0.strftime("%Y-%m-%d %H:%M:%S"),
    }
