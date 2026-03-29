# ==============================================================
# stock_screener.py  v3.0  (S-2 / S-3 統合)
# 日本株・米国株 個別銘柄スクリーニング
# ==============================================================
# 【変更履歴】
#   v2.0: PER/PBR/ROE/時価総額/出来高フィルター + composite_score算出
#   v3.0: [S-2] EDINETアクルーアル統合（quality_scoreにSloan補正）
#         [S-3] 未発見銘柄スコアリング（discovery_score列追加）
#
# 【スクリーニング仕様】
#   Step 1: セクタースコア上位 N 業種を選択
#   Step 2: 各業種の構成銘柄を yfinance で財務取得
#   Step 3: フィルター条件（PER / PBR / ROE / 時価総額 / 出来高）でパス/フェイルを判定
#   Step 4: 通過銘柄をスコアでソートして上位 M 件を返す
#
# 【スコア算出（v3.0）】
#   composite_score = セクタースコア(0-100) * 0.40
#                   + バリュースコア(0-100) * 0.30    ← PER/PBR逆数
#                   + クォリティスコア(0-100) * 0.30  ← ROE + EDINET accrual補正
#
#   [S-2] quality_score補正（Sloan 1996）:
#     EDINETアクルーアル比率が業種平均より低い銘柄 → quality_score にボーナス
#     tier AAA (accrual < -0.05):  +20pt
#     tier AA  (accrual < -0.02):  +12pt
#     tier A   (accrual < 0.02 ): +6pt
#     tier B   (accrual < 0.05 ): 0pt（ペナルティなし）
#     tier C   (accrual >= 0.05): -15pt（利益水増し疑い）
#
# 【出力スキーマ（v3.0追加フィールド）】
#   既存フィールド: ticker, name, market, sector_code, sector_name,
#                   per, pbr, roe, market_cap_b, avg_volume,
#                   pass_filters, filter_detail,
#                   sector_score, value_score, quality_score, composite_score
#   v3.0追加:
#     accrual_ratio:        float|None  ← EDINETから取得
#     accrual_tier:         str         ← "AAA"/"AA"/"A"/"B"/"C"/"N/A"
#     accrual_bonus:        float       ← quality_scoreへの加算点
#     analyst_count:        int|None    ← numberOfAnalystOpinions
#     discovery_score:      float       ← [S-3] 未発見銘柄スコア(0-100)
#     discovery_flags:      list[str]   ← スコアの根拠フラグリスト
#
# 【依存】yfinance / config.py / edinet_rotation_manager.py
# 【呼出】from stock_screener import run_stock_screener
# ==============================================================

import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: 銘柄マスタ
# ==============================================================

JP_SECTOR_TICKERS = {
    "1001": ["1332.T", "1301.T", "1377.T"],
    "1002": ["1662.T", "1605.T", "1642.T"],
    "1003": ["1802.T", "1803.T", "1812.T", "1808.T", "1820.T"],
    "1004": ["2002.T", "2269.T", "2282.T", "2503.T", "2801.T",
             "2802.T", "2914.T"],
    "1005": ["3401.T", "3402.T", "3103.T"],
    "1006": ["3861.T", "3893.T", "3941.T"],
    "1007": ["4004.T", "4005.T", "4063.T", "4183.T", "4188.T", "4208.T"],
    "1008": ["4151.T", "4502.T", "4503.T", "4506.T", "4507.T",
             "4519.T", "4523.T", "4568.T"],
    "1009": ["5001.T", "5002.T", "5019.T"],
    "1010": ["5108.T", "5101.T"],
    "1011": ["5201.T", "5202.T", "5233.T", "5301.T"],
    "1012": ["5401.T", "5406.T", "5411.T", "5713.T"],
    "1013": ["5711.T", "5714.T", "5706.T", "5707.T"],
    "1014": ["5631.T", "5901.T", "3436.T"],
    "1015": ["6273.T", "6301.T", "6302.T", "6305.T", "6326.T",
             "6361.T", "6366.T", "7011.T"],
    "1016": ["6501.T", "6502.T", "6504.T", "6594.T", "6645.T",
             "6674.T", "6701.T", "6702.T", "6723.T", "6752.T",
             "6758.T", "6762.T", "6857.T", "8035.T"],
    "1017": ["7201.T", "7202.T", "7203.T", "7205.T", "7211.T",
             "7261.T", "7267.T", "7269.T", "7270.T"],
    "1018": ["4902.T", "7733.T", "7741.T", "7731.T", "6146.T"],
    "1019": ["7832.T", "7974.T", "9684.T", "6460.T"],
    "1020": ["9501.T", "9502.T", "9503.T", "9531.T", "9532.T"],
    "1021": ["9001.T", "9005.T", "9007.T", "9008.T", "9009.T"],
    "1022": ["9101.T", "9104.T", "9107.T"],
    "1023": ["9202.T", "9201.T"],
    "1024": ["9301.T", "9064.T"],
    "1025": ["4307.T", "4704.T", "9613.T", "9432.T", "9433.T",
             "9434.T", "9984.T", "3659.T"],
    "1026": ["2768.T", "8001.T", "8002.T", "8031.T", "8053.T", "8058.T"],
    "1027": ["2651.T", "2678.T", "3382.T", "7186.T", "8267.T",
             "9843.T", "9983.T"],
    "1028": ["8306.T", "8308.T", "8309.T", "8316.T", "8411.T"],
    "1029": ["8601.T", "8604.T", "8616.T"],
    "1030": ["8725.T", "8750.T", "8766.T"],
    "1031": ["8515.T", "8566.T", "8572.T", "8591.T"],
    "1032": ["3003.T", "8801.T", "8802.T", "8804.T", "8830.T"],
    "1033": ["2432.T", "4661.T", "6098.T", "9602.T", "9735.T"],
}

# [S-3] 未発見銘柄スコアの判定しきい値
DISCOVERY_MCAP_MAX_B      = 500.0   # 時価総額500億円以下
DISCOVERY_ANALYST_MAX     = 3       # アナリスト数3人未満
DISCOVERY_VOLUME_RATIO    = 0.5     # 業種平均出来高の0.5倍以下
DISCOVERY_SCORE_THRESHOLD = 0.80    # composite_score上位20%相当（スコア比率）

# [S-2] EDINETアクルーアルのtierしきい値（Sloan 1996準拠）
ACCRUAL_TIERS = [
    # (upper_bound, tier_label, quality_bonus)
    (-0.05, "AAA", 20.0),   # キャッシュフロー圧倒的超過 → 最高品質
    (-0.02, "AA",  12.0),   # キャッシュフロー超過 → 高品質
    ( 0.02, "A",    6.0),   # ほぼ均衡 → 良好
    ( 0.05, "B",    0.0),   # わずかにアクルーアル超過 → 許容範囲
    ( 9999, "C",  -15.0),   # アクルーアル高い → 利益水増し疑い
]


# ==============================================================
# BLOCK 1: yfinanceデータ取得（v3.0: analyst_count追加）
# ==============================================================

def _fetch_ticker_data(ticker: str) -> Optional[dict]:
    """
    1銘柄の財務データを yfinance で取得する。

    v3.0変更: numberOfAnalystOpinions（アナリスト数）を追加取得

    Returns:
        {ticker, name, per, pbr, roe, market_cap_b, avg_volume, analyst_count}
        取得失敗時は None
    """
    try:
        import yfinance as yf
        import numpy as np

        t    = yf.Ticker(ticker)
        info = t.info

        if not info or info.get("regularMarketPrice") is None:
            return None

        per        = info.get("trailingPE")
        pbr        = info.get("priceToBook")
        roe        = info.get("returnOnEquity")
        market_cap = info.get("marketCap") or 0
        name       = info.get("longName") or info.get("shortName") or ticker
        avg_vol    = info.get("averageVolume") or info.get("averageVolume10days") or 0
        # 在庫回転率 = 売上高 / 棚卸資産（製造業・小売業で有効）
        revenue   = info.get("totalRevenue")
        inventory = info.get("inventory")
        inv_turn  = None
        if revenue and inventory:
            try:
                inv_f = float(inventory)
                rev_f = float(revenue)
                if inv_f > 0:
                    inv_turn = round(rev_f / inv_f, 2)
            except (TypeError, ValueError):
                pass
        # [S-3] アナリストカバレッジ数
        analyst_count = info.get("numberOfAnalystOpinions")
        if analyst_count is not None:
            try:
                analyst_count = int(analyst_count)
            except (TypeError, ValueError):
                analyst_count = None

        def _clean(v):
            if v is None:
                return None
            try:
                f = float(v)
                return None if (f != f) else f
            except (TypeError, ValueError):
                return None

        return {
            "ticker":         ticker,
            "name":           name[:40],
            "per":            _clean(per),
            "pbr":            _clean(pbr),
            "roe":            _clean(roe),
            "market_cap_b":   round(market_cap / 1e8, 1),
            "avg_volume":     int(avg_vol),
            "analyst_count":  analyst_count,   # v3.0追加
            "inventory_turnover": inv_turn,
        }
    except Exception:
        return None


# ==============================================================
# BLOCK 1.5: [S-2] EDINETアクルーアルデータ読み込み
# ==============================================================

def _load_edinet_accruals(cfg) -> dict:
    """
    edinet_rotation_manager の蓄積DBからアクルーアル比率を読み込む。

    edinet_client._fetch_financials() が保存した
    analysis/{E_CODE}.json の accrual_ratio を
    {ticker: accrual_ratio} 形式で返す。

    Returns:
        {ticker: float}  ← 例: {"6758.T": -0.031, "8306.T": 0.012}
        取得できない場合は空辞書 {}
    """
    try:
        import sys, importlib
        sys.path.insert(0, str(Path(__file__).parent))
        _erm = importlib.import_module("edinet_rotation_manager")
        db   = _erm.load_db(cfg)
    except Exception as e:
        print(f"  ⚠️  EDINET accrual読み込みエラー（スキップ）: {e}")
        return {}

    accruals = {}
    meta     = db.get("meta", {})
    analysis = db.get("analysis", {})   # {ticker: analysis_dict}

    for ticker, adict in analysis.items():
        # v2.0形式: analysis辞書内に直接 accrual_ratio がある場合
        if isinstance(adict, dict):
            acr = adict.get("accrual_ratio")
            if acr is not None:
                try:
                    accruals[ticker] = float(acr)
                    continue
                except (TypeError, ValueError):
                    pass

            # financials形式: adict["financials"]["accrual_ratio"]
            fin = adict.get("financials") or adict.get("data", {})
            if isinstance(fin, dict):
                acr = fin.get("accrual_ratio")
                if acr is not None:
                    try:
                        accruals[ticker] = float(acr)
                    except (TypeError, ValueError):
                        pass

    n = len(accruals)
    if n > 0:
        print(f"  📊 EDINET accrual: {n}銘柄分のデータを読み込み")
    else:
        print("  ⏩ EDINET accrual: データなし（EDINETが未蓄積または無効）")

    return accruals


def _calc_accrual_tier(accrual: Optional[float]) -> tuple[str, float]:
    """
    アクルーアル比率からtierラベルとquality_scoreボーナスを返す。

    Args:
        accrual: アクルーアル比率 (純利益-営業CF)/総資産

    Returns:
        (tier: str, bonus: float)
        例: ("AA", 12.0) / ("N/A", 0.0)
    """
    if accrual is None:
        return "N/A", 0.0
    for upper, label, bonus in ACCRUAL_TIERS:
        if accrual < upper:
            return label, bonus
    return "C", -15.0


def _calc_sector_accrual_mean(tickers: list, edinet_accruals: dict) -> Optional[float]:
    """
    業種内銘柄のアクルーアル比率の平均を計算する（業種相対評価用）。
    """
    vals = [edinet_accruals[t] for t in tickers if t in edinet_accruals]
    return sum(vals) / len(vals) if vals else None


# ==============================================================
# BLOCK 2: フィルター & スコア算出（v3.0: accrual_bonus統合）
# ==============================================================

def _apply_filters(data: dict, cfg) -> tuple:
    """
    フィルター条件を適用する。

    Returns:
        (passed: bool, detail: dict)
    """
    per_max    = getattr(cfg, "SCREEN_PER_MAX",    30.0)
    pbr_max    = getattr(cfg, "SCREEN_PBR_MAX",     5.0)
    roe_min    = getattr(cfg, "SCREEN_ROE_MIN",     0.08)
    mcap_min_b = getattr(cfg, "SCREEN_MCAP_MIN_B", 100.0)
    vol_min    = getattr(cfg, "SCREEN_VOL_MIN",    100_000)

    detail = {}
    fails  = []

    per = data.get("per")
    if per is None:
        detail["per"] = "N/A"
    elif per <= 0 or per > per_max:
        detail["per"] = f"NG({per:.1f})"
        fails.append("per")
    else:
        detail["per"] = f"OK({per:.1f})"

    pbr = data.get("pbr")
    if pbr is None:
        detail["pbr"] = "N/A"
    elif pbr <= 0 or pbr > pbr_max:
        detail["pbr"] = f"NG({pbr:.1f})"
        fails.append("pbr")
    else:
        detail["pbr"] = f"OK({pbr:.1f})"

    roe = data.get("roe")
    if roe is None:
        detail["roe"] = "N/A"
    elif roe < roe_min:
        detail["roe"] = f"NG({roe*100:.1f}%)"
        fails.append("roe")
    else:
        detail["roe"] = f"OK({roe*100:.1f}%)"

    mcap = data.get("market_cap_b", 0)
    if mcap < mcap_min_b:
        detail["market_cap"] = f"NG({mcap:.0f}億)"
        fails.append("market_cap")
    else:
        detail["market_cap"] = f"OK({mcap:.0f}億)"

    vol = data.get("avg_volume", 0)
    if vol < vol_min:
        detail["volume"] = f"NG({vol:,})"
        fails.append("volume")
    else:
        detail["volume"] = f"OK({vol:,})"

    return (len(fails) == 0), detail


def _calc_scores(
    data:             dict,
    sector_score:     float,
    edinet_accrual:   Optional[float] = None,
    sector_accrual_mean: Optional[float] = None,
    surprise_bonus: float = 0.0
) -> dict:
    """
    バリュースコア / クォリティスコア / 総合スコアを算出する。

    v3.0変更:
        edinet_accrual が渡された場合、Sloan(1996)アクルーアル補正を
        quality_score に加算する。
        業種平均より低いアクルーアル比率 = 利益の質が高い = ボーナス。

    Args:
        data:                yfinanceデータ辞書
        sector_score:        業種の composite_score
        edinet_accrual:      当該銘柄のEDINETアクルーアル比率 (nullable)
        sector_accrual_mean: 業種平均アクルーアル比率 (nullable)

    Returns:
        {sector_score, value_score, quality_score, composite_score,
         accrual_ratio, accrual_tier, accrual_bonus}
    """
    per = data.get("per")
    pbr = data.get("pbr")
    roe = data.get("roe")

    # バリュースコア (0-100)
    if per and per > 0:
        per_score = max(0, min(100, (30 - per) / 20 * 100))
    else:
        per_score = 50

    if pbr and pbr > 0:
        pbr_score = max(0, min(100, (5 - pbr) / 4 * 100))
    else:
        pbr_score = 50

    value_score = round((per_score * 0.6 + pbr_score * 0.4), 1)
    inv_bonus = 0.0   # if roe ブロック外で初期化（ROEなし銘柄でも参照できるよう）
    # クォリティスコア base (0-100): ROEベース
    if roe is not None:
        quality_base = max(0, min(100, roe / 0.30 * 100))
    else:
        quality_base = 50.0
    # [A-7] 決算サプライズボーナス（+5 〜 -5pt）
    # 在庫回転率ボーナス（製造・小売に有意 / 金融・サービスはN/A）
        inv_turn  = data.get("inventory_turnover")
        if inv_turn is not None:
            if   inv_turn >= 12: inv_bonus =  4.0   # 月1回転以上 = 優良
            elif inv_turn >= 6:  inv_bonus =  2.0   # 2ヶ月に1回転 = 良好
            elif inv_turn <= 2:  inv_bonus = -3.0   # 半年に1回転 = 過剰在庫
    # [S-2] アクルーアル補正（Sloan 1996）
    # - 銘柄個別のアクルーアルtierでボーナス算出
    # - 業種平均より低い場合（=相対的に優良）はさらに+5pt上乗せ
    acr_tier, acr_bonus = _calc_accrual_tier(edinet_accrual)

    relative_bonus = 0.0
    if (edinet_accrual is not None
            and sector_accrual_mean is not None
            and edinet_accrual < sector_accrual_mean - 0.02):
        # 業種平均より2pt以上低い = 相対的に利益品質が高い
        relative_bonus = 5.0

    quality_score = round(
        max(0, min(100,
            quality_base + acr_bonus + relative_bonus
            + surprise_bonus + inv_bonus   # ★ surprise_bonus と inv_bonus を統合
        )),
        1,
    )

    # 総合スコア
    composite = round(
        sector_score * 0.40
        + value_score  * 0.30
        + quality_score * 0.30,
        1,
    )

    return {
        "sector_score":    round(sector_score, 1),
        "value_score":     value_score,
        "quality_score":   quality_score,
        "composite_score": composite,
        "accrual_ratio":   edinet_accrual,   # v3.0追加
        "accrual_tier":    acr_tier,         # v3.0追加
        "accrual_bonus":   round(acr_bonus + relative_bonus, 1),  # v3.0追加
        "surprise_bonus":   surprise_bonus,
    }


# ==============================================================
# BLOCK 2.5: [S-3] 未発見銘柄スコアリング
# ==============================================================

def _calc_discovery_score(
    data:            dict,
    composite_score: float,
    score_threshold: float,
    sector_vol_mean: Optional[float] = None,
) -> tuple[float, list]:
    """
    「市場が気づいていない」銘柄の未発見スコアを計算する。

    4条件の充足度を0〜100点で評価:
      1. composite_score が上位20%相当（score_threshold超え）
      2. 出来高が業種平均の0.5倍以下（低流動性）
      3. アナリストカバレッジ3人未満
      4. 時価総額500億円以下（小型株）

    各条件の充足でフラグを立て、充足数に応じてスコアを付与:
      4条件充足: 100点（真の未発見銘柄）
      3条件充足:  75点
      2条件充足:  40点
      1条件充足:  10点
      0条件充足:   0点

    score_thresholdを超えていない場合は最大20点（参考値）。

    Returns:
        (discovery_score: float, flags: list[str])
        flags: 充足した条件のラベルリスト
    """
    flags   = []
    mcap_b  = data.get("market_cap_b", 0) or 0
    vol     = data.get("avg_volume", 0) or 0
    analyst = data.get("analyst_count")

    # 条件1: 高スコア（業種内上位20%）
    is_high_score = composite_score >= score_threshold
    if is_high_score:
        flags.append(f"高スコア({composite_score:.0f}点)")

    # 条件2: 低出来高（業種平均の0.5倍以下）
    is_low_volume = False
    if sector_vol_mean and sector_vol_mean > 0:
        if vol <= sector_vol_mean * DISCOVERY_VOLUME_RATIO:
            is_low_volume = True
            flags.append(f"低出来高({vol:,}株≦平均×{DISCOVERY_VOLUME_RATIO})")
    else:
        # 業種平均不明の場合: 絶対値で判断（10万株以下）
        if 0 < vol <= 100_000:
            is_low_volume = True
            flags.append(f"低出来高({vol:,}株)")

    # 条件3: アナリストカバレッジ少
    is_low_coverage = False
    if analyst is not None and analyst < DISCOVERY_ANALYST_MAX:
        is_low_coverage = True
        flags.append(f"アナリスト{analyst}人")
    elif analyst is None:
        # カバレッジデータなし = 未カバー可能性が高い
        is_low_coverage = True
        flags.append("アナリスト不明（未カバー可能性）")

    # 条件4: 小型株（時価総額500億円以下）
    is_small_cap = 0 < mcap_b <= DISCOVERY_MCAP_MAX_B
    if is_small_cap:
        flags.append(f"小型株({mcap_b:.0f}億円)")

    # スコア算出（高スコアが前提条件）
    n = sum([is_high_score, is_low_volume, is_low_coverage, is_small_cap])
    if not is_high_score:
        # 高スコアでなければ未発見銘柄として意味がない（上限20点）
        score_map = {0: 0, 1: 5, 2: 10, 3: 15, 4: 20}
    else:
        score_map = {0: 0, 1: 10, 2: 40, 3: 75, 4: 100}

    return float(score_map.get(n, 0)), flags


# ==============================================================
# BLOCK 3: 業種スクリーニング（v3.1: 決算サプライズ & EDINET統合版）
# ==============================================================

def _screen_sector(sector_code: str, sector_name: str,
                   sector_score: float, tickers: list,
                   cfg, market: str = "JP",
                   surprise_cache: dict = None,
                   edinet_accruals: dict = None,
                   all_scores_ref: list = None,
                   credit_cache: dict = None,
                   tech_cache: dict = None,
                   insider_cache: dict = None,   # ← 追加
                   dividend_cache: dict = None,  # ← 追加
                   sr_cache: dict = None,
                   vp_cache: dict = None,
                   edinet_quality_cache: dict = None,
                   comps_cache: dict = None,
                   dcf_cache: dict = None
                   ) -> list:
    """
    1 業種の銘柄リストをスクリーニングする。
    """
    accruals    = edinet_accruals or {}
    candidates  = []

    # 業種平均アクルーアル（S-2: 相対評価用）
    sector_accrual_mean = _calc_sector_accrual_mean(tickers, accruals)

    # 1. 業種内全銘柄の基礎データを収集
    raw_data_list = []
    print(f"    [{sector_code}] {sector_name}: {len(tickers)}銘柄チェック中...")

    for ticker in tickers:
        data = _fetch_ticker_data(ticker)
        "current_price": _clean(info.get("regularMarketPrice")),
        if data is not None:
            raw_data_list.append((ticker, data))
        time.sleep(0.2)

    # 業種平均出来高算出（S-3: 未発見スコア用）
    vols = [d.get("avg_volume", 0) for (_, d) in raw_data_list if d.get("avg_volume")]
    sector_vol_mean = sum(vols) / len(vols) if vols else None

    # 2. 決算サプライズ用モジュールの動的読み込み（ループ外で1回のみ）
    es_module = None
    if surprise_cache:
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            es_module = _imp.import_module("earnings_surprise")
        except Exception:
            pass

    # 3. メインループ：フィルタリングとスコアリング
    for ticker, data in raw_data_list:
        # A. フィルター判定（1回のみ実行）
        passed, detail = _apply_filters(data, cfg)
        
        # B. 各種ボーナスの集計
        total_bonus = 0.0
        cr_label = ""
        inv_bonus = 0.0
        
        # 決算サプライズボーナス
        es_bonus = 0.0
        if surprise_cache:
            try:
                import sys as _sys, importlib as _imp
                _sys.path.insert(0, str(Path(__file__).parent))
                _es_mod = _imp.import_module("earnings_surprise")
                es_bonus = _es_mod.get_screener_bonus(ticker, surprise_cache)
                total_bonus += es_bonus
            except Exception:
                pass

        # 信用倍率ボーナス
        if credit_cache:
            try:
                _cd_mod = _imp.import_module("credit_data")
                cr_bonus, cr_label = _cd_mod.get_credit_bonus(ticker, credit_cache)
                total_bonus += cr_bonus
            except Exception:
                pass
        # [NEW] EDINET品質ボーナス
        eq_bonus = 0.0
        if edinet_quality_cache and ticker in edinet_quality_cache:
            try:
                _sfl_mod = _imp.import_module("screener_feedback_loop")
                eq_bonus = _sfl_mod.get_edinet_bonus(ticker, edinet_quality_cache)
            except Exception:
                pass
        scores = _calc_scores(data, sector_score,
                              surprise_bonus=es_bonus + cr_bonus + im_bonus
                                             + div_bonus + eq_bonus)
        # [5-2] comps相対バリューボーナス（value_scoreのブレンド）
        if comps_cache and ticker in comps_cache:
            try:
                _ca_mod = _imp.import_module("comps_analyzer")
                cb = _ca_mod.get_comps_bonus(ticker, comps_cache)
                if cb != 0:
                    old_vs = scores.get("value_score", 50) or 50
                    new_vs = round(old_vs * 0.7 + comps_cache[ticker].get("comps_score", 50) * 0.3, 1)
                    scores["value_score"] = new_vs
                    scores["comps_bonus"] = cb
                    scores["composite_score"] = round(
                        scores.get("sector_score", 50)    * 0.35
                        + new_vs                          * 0.25
                        + scores.get("quality_score", 50) * 0.20
                        + scores.get("technical_score", 50) * 0.20, 1)
            except Exception:
                pass
        # [6-3] DCFボーナス（value_scoreに直接加算）
        if dcf_cache and ticker in dcf_cache:
            try:
                _dcf_mod = _imp.import_module("dcf_valuation")
                db = _dcf_mod.get_dcf_bonus(ticker, dcf_cache)
                if db != 0:
                    scores["dcf_bonus"]   = db
                    scores["value_score"] = round(
                        max(0, min(100, scores.get("value_score", 50) + db)), 1)
                    scores["composite_score"] = round(
                        scores.get("sector_score", 50)    * 0.35
                        + scores["value_score"]           * 0.25
                        + scores.get("quality_score", 50) * 0.20
                        + scores.get("technical_score",50) * 0.20, 1)
            except Exception:
                pass

        # インサイダーボーナス
        if insider_cache:
            try:
                _im_mod = _imp.import_module("insider_monitor")
                total_bonus += _im_mod.get_insider_bonus(ticker, insider_cache)
            except Exception:
                pass

        # 配当ボーナス
        if dividend_cache:
            try:
                _dc_mod = _imp.import_module("dividend_calendar")
                total_bonus += _dc_mod.get_dividend_bonus(ticker, dividend_cache)
            except Exception:
                pass

        # C. EDINETアクルーアルの取得
        edinet_acr = accruals.get(ticker)

        # D. 総合スコア計算（すべてのボーナスを統合して1回だけ実行）
        scores = _calc_scores(
            data                = data,
            sector_score        = sector_score,
            edinet_accrual      = edinet_acr,
            sector_accrual_mean = sector_accrual_mean,
            surprise_bonus      = total_bonus
        )

        # [4-6] SR ボーナスを technical_score に加算
        if sr_cache and ticker in sr_cache:
            try:
                _sr_mod = _imp.import_module("support_resistance")
                sr_bonus = _sr_mod.get_sr_bonus(ticker, sr_cache)
                if sr_bonus != 0:
                    # technical_score に sr_bonus を加算して composite_score を再計算
                    ts = scores.get("technical_score", 50.0) or 50.0
                    new_ts = round(max(0, min(100, ts + sr_bonus)), 1)
                    scores["technical_score"] = new_ts
                    scores["sr_bonus"] = sr_bonus
                    scores["composite_score"] = round(
                        scores.get("sector_score", 50) * 0.35
                        + scores.get("value_score",  50) * 0.25
                        + scores.get("quality_score", 50) * 0.20
                        + new_ts * 0.20, 1)
            except Exception:
                pass
        # [4-7] VP ボーナスを technical_score に加算
        if vp_cache and ticker in vp_cache:
            try:
                _vp_mod = _imp.import_module("volume_profile")
                vp_bonus = _vp_mod.get_vp_bonus(ticker, vp_cache)
                if vp_bonus != 0:
                    ts_current = scores.get("technical_score", 50.0) or 50.0
                    new_ts_vp  = round(max(0, min(100, ts_current + vp_bonus)), 1)
                    scores["technical_score"] = new_ts_vp
                    scores["vp_bonus"] = vp_bonus
                    scores["composite_score"] = round(
                        scores.get("sector_score", 50) * 0.35
                        + scores.get("value_score",  50) * 0.25
                        + scores.get("quality_score", 50) * 0.20
                        + new_ts_vp * 0.20, 1)
            except Exception:
                pass

        # テクニカルスコア統合
        if tech_cache and ticker in tech_cache:
            try:
                _ts_mod = _imp.import_module("technical_scorer")
                scores = _ts_mod.integrate_technical_score(
                    {**data, **scores}, tech_cache[ticker]
                )
            except Exception:
                pass

        candidate = {
            "ticker":         ticker,
            "name":           data["name"],
            "market":         market,
            "sector_code":    sector_code,
            "sector_name":    sector_name,
            "per":            data.get("per"),
            "pbr":            data.get("pbr"),
            "roe":            round(data["roe"] * 100, 1) if data.get("roe") is not None else None,
            "market_cap_b":   data.get("market_cap_b"),
            "avg_volume":     data.get("avg_volume"),
            "analyst_count":  data.get("analyst_count"),
            "pass_filters":   passed,
            "filter_detail":  detail,
            **scores,
            "surprise_bonus": es_bonus,
            "discovery_score": 0.0,
            "discovery_flags": [],
            "credit_flag":    cr_label,
            "inventory_turnover": data.get("inventory_turnover"),
            "inv_bonus":      scores.get("inv_bonus", 0.0), # scoresから取得するか、別途計算するか仕様に合わせて調整
            "sr_bonus":  scores.get("sr_bonus", 0.0),
            "vp_bonus":     scores.get("vp_bonus", 0.0),
            "edinet_bonus": eq_bonus,
            "current_price": data.get("current_price"),
            "comps_bonus": scores.get("comps_bonus", 0.0),
            "dcf_bonus": scores.get("dcf_bonus", 0.0),
        }
        candidates.append(candidate)

        # 全銘柄スコア参照リストに追加
        if all_scores_ref is not None:
            all_scores_ref.append(scores["composite_score"])

    # ログ出力
    passed_list = [c for c in candidates if c["pass_filters"]]
    print(f"      → 通過: {len(passed_list)}/{len(candidates)}銘柄"
          + (f" | EDINET accrual: {sum(1 for t,_ in raw_data_list if t in accruals)}銘柄"
             if accruals else ""))

    return candidates


# ==============================================================
# BLOCK 4: レポート保存（v3.0: discovery_score列追加）
# ==============================================================

def _save_screener_report(result: dict, cfg) -> None:
    """insights/screener/screener_YYYY-MM-DD.md に保存する。"""
    today      = datetime.now().strftime("%Y-%m-%d")
    screen_dir = Path(getattr(cfg, "INSIGHT_DIR", "insights")) / "screener"

    try:
        screen_dir.mkdir(parents=True, exist_ok=True)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# スクリーニングレポート（{today}）",
            f"**生成日時**: {now}  |  "
            f"**スクリーニング対象**: {result['screened_count']}銘柄  |  "
            f"**通過**: {result['pass_count']}銘柄",
            "",
            "## 📊 候補銘柄（composite_score 降順）",
            "",
            "| Ticker | 銘柄名 | 業種 | PER | PBR | ROE(%) | accrual | スコア | 未発見 |",
            "|--------|--------|------|-----|-----|--------|---------|--------|--------|",
        ]

        passed = [c for c in result["candidates"] if c["pass_filters"]]
        passed.sort(key=lambda x: x["composite_score"], reverse=True)

        for c in passed[:30]:
            per   = f"{c['per']:.1f}" if c["per"] else "N/A"
            pbr   = f"{c['pbr']:.1f}" if c["pbr"] else "N/A"
            roe   = f"{c['roe']:.1f}" if c["roe"] is not None else "N/A"
            acr   = f"{c['accrual_ratio']:.3f}({c['accrual_tier']})" if c.get("accrual_ratio") is not None else "N/A"
            disc  = f"{c.get('discovery_score', 0):.0f}"
            lines.append(
                f"| {c['ticker']} | {c['name'][:20]} | {c['sector_name'][:8]} "
                f"| {per} | {pbr} | {roe} | {acr} | {c['composite_score']:.1f} | {disc} |"
            )

        # 未発見銘柄トップ10
        discovery_top = sorted(
            [c for c in result["candidates"] if c.get("discovery_score", 0) >= 40],
            key=lambda x: x.get("discovery_score", 0),
            reverse=True,
        )[:10]
        if discovery_top:
            lines += [
                "",
                "## 🔍 未発見銘柄候補（discovery_score ≥ 40）",
                "",
                "| Ticker | 銘柄名 | スコア | 未発見スコア | 根拠 |",
                "|--------|--------|--------|------------|------|",
            ]
            for c in discovery_top:
                flags = " / ".join(c.get("discovery_flags", []))
                lines.append(
                    f"| {c['ticker']} | {c['name'][:20]} | {c['composite_score']:.1f} "
                    f"| {c['discovery_score']:.0f} | {flags} |"
                )

        out = screen_dir / f"screener_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 スクリーニングレポート保存: screener_{today}.md")

    except Exception as e:
        print(f"  ⚠️  スクリーニングレポート保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 5: メインエントリポイント（v3.0: S-2/S-3統合）
# ==============================================================

def run_stock_screener(cfg, sector_results: dict) -> dict:
    """
    セクター分析結果を受け取り、個別銘柄スクリーニングを実行する。

    v3.0変更:
        - EDINETアクルーアルデータを事前ロード（S-2）
        - 全銘柄スクリーニング後に discovery_score を付与（S-3）
        - dashboard_latest.csv に discovery_score / accrual_tier 列が追加される

    Args:
        cfg            : config モジュール
        sector_results : results["sectors"] の dict

    Returns:
        {
          "candidates":       全銘柄データ（フィルター通過・不通過を含む）,
          "screened_count":   int,
          "pass_count":       int,
          "top_sectors":      [sector_name, ...],
          "discovery_top":    discovery_score上位銘柄リスト,
          "timestamp":        str,
        }
    """
    print("\n  📋 個別銘柄スクリーニング（Step C v3.0）開始...")
    t0 = datetime.now()
    # _imp を先頭で一括 import（line 863/873 での NameError を防ぐ）
    import sys as _sys, importlib as _imp
    _sys.path.insert(0, str(Path(__file__).parent))
    # [4-2] インサイダーボーナスキャッシュ
    insider_cache = {}
    if getattr(cfg, "ENABLE_INSIDER_MONITOR", True):
        try:
            _im_mod = _imp.import_module("insider_monitor")
            _im_raw = _im_mod._load_cache(cfg)
            if _im_raw:
                insider_cache = _im_raw
        except Exception:
            pass
    # [4-3] 配当ボーナスキャッシュ
    dividend_cache = {}
    if getattr(cfg, "ENABLE_DIVIDEND_CALENDAR", True):
        try:
            _dc_mod = _imp.import_module("dividend_calendar")
            _dc_raw = _dc_mod._load_cache(cfg)
            if _dc_raw:
                dividend_cache = _dc_raw
        except Exception:
            pass

    # [A-7] 決算サプライズキャッシュ読み込み（当日分 / 24時間有効）
    surprise_cache = {}
    if getattr(cfg, "ENABLE_EARNINGS_SURPRISE", True):
        try:
            _es_mod = _imp.import_module("earnings_surprise")
            _es_cache = _es_mod._load_cache(cfg)
            if _es_cache:
                surprise_cache = _es_cache
                print(f"    📋 決算サプライズキャッシュ: {len(surprise_cache)}銘柄")
        except Exception:
            pass
    # [NEW] EDINETフィードバック品質キャッシュ読み込み
    edinet_quality_cache = {}
    if getattr(cfg, "ENABLE_SCREENER_FEEDBACK", True):
        try:
            _sfl_mod = _imp.import_module("screener_feedback_loop")
            edinet_quality_cache = _sfl_mod._load_cache(cfg)
            if edinet_quality_cache:
                print(f"    🔄 EDINET品質キャッシュ: {len(edinet_quality_cache)}銘柄")
        except Exception:
            pass
    # [5-2] comps キャッシュ読み込み
    comps_cache = {}
    if getattr(cfg, "ENABLE_COMPS_ANALYZER", True):
        try:
            _ca_mod = _imp.import_module("comps_analyzer")
            comps_cache = _ca_mod._load_cache(cfg)
            if comps_cache:
                print(f"    📊 compsキャッシュ: {len(comps_cache)}銘柄")
        except Exception:
            pass
    # [6-3] DCFキャッシュ読み込み
    dcf_cache = {}
    if getattr(cfg, "ENABLE_DCF_VALUATION", True):
        try:
            _dcf_mod = _imp.import_module("dcf_valuation")
            dcf_cache = _dcf_mod._load_cache(cfg)
            if dcf_cache:
                print(f"    📐 DCFキャッシュ: {len(dcf_cache)}銘柄")
        except Exception:
            pass
    
    # [4-6] SR キャッシュ読み込み
    sr_cache = {}
    if getattr(cfg, "ENABLE_SUPPORT_RESISTANCE", True):
        try:
            _sr_mod = _imp.import_module("support_resistance")
            sr_cache = _sr_mod._load_cache(cfg)
            if sr_cache:
                print(f"    📐 SRキャッシュ: {len(sr_cache)}銘柄")
        except Exception:
            pass
    # [4-7] VP キャッシュ読み込み
    vp_cache = {}
    if getattr(cfg, "ENABLE_VOLUME_PROFILE", True):
        try:
            _vp_mod = _imp.import_module("volume_profile")
            vp_cache = _vp_mod._load_cache(cfg)
            if vp_cache:
                print(f"    📊 VPキャッシュ: {len(vp_cache)}銘柄")
        except Exception:
            pass

    top_n_sectors = getattr(cfg, "SCREEN_TOP_N_SECTORS", 5)

    if not sector_results:
        print("  ⚠️  sector_results が空のためスクリーニングをスキップ")
        return {
            "candidates": [], "screened_count": 0, "pass_count": 0,
            "top_sectors": [], "discovery_top": [],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    # [3-5] 信用残高キャッシュ読み込み（週次データ / 7日有効）
    credit_cache = {}
    if getattr(cfg, "ENABLE_CREDIT_DATA", True):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _cd_mod = _imp.import_module("credit_data")
            _cd_raw = _cd_mod._load_cache(cfg)
            if _cd_raw:
                credit_cache = _cd_raw.get("records", {})
                print(f"    📊 信用残高キャッシュ: {len(credit_cache)}銘柄")
        except Exception:
            pass
    # [4-1] テクニカルスコアキャッシュ読み込み
    tech_cache = {}
    if getattr(cfg, "ENABLE_TECHNICAL_SCORER", True):
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _ts_mod = _imp.import_module("technical_scorer")
            tech_cache = _ts_mod._load_cache(cfg)
            if tech_cache:
                print(f"    📈 テクニカルキャッシュ: {len(tech_cache)}銘柄")
        except Exception:
            pass
# [S-2] EDINETアクルーアルデータを事前ロード
    edinet_accruals = {}
    try:
        edinet_accruals = _load_edinet_accruals(cfg)
    except Exception as e:
        print(f"  ⚠️  EDINET accrual ロード失敗（スキップ）: {e}")
    # セクタースコア上位 N 業種を選択
    sorted_sectors = sorted(
        sector_results.items(),
        key=lambda x: x[1].get("composite_score", 0) if isinstance(x[1], dict) else 0,
        reverse=True,
    )[:top_n_sectors]

    top_sector_names = [
        v.get("sector_name_ja", k) if isinstance(v, dict) else str(k)
        for k, v in sorted_sectors
    ]
    print(f"  対象業種（上位{top_n_sectors}）: {' / '.join(top_sector_names)}")

    all_candidates  = []
    all_comp_scores = []   # [S-3] 全銘柄スコアを蓄積（閾値計算用）

    for code, sec_data in sorted_sectors:
        if not isinstance(sec_data, dict):
            continue
        sector_score = sec_data.get("composite_score", 50.0)
        sector_name  = sec_data.get("sector_name_ja", code)
        tickers      = JP_SECTOR_TICKERS.get(code, [])

        if not tickers:
            print(f"    [{code}] {sector_name}: 銘柄マスタなし → スキップ")
            continue

        candidates = _screen_sector(
            sector_code     = code,
            sector_name     = sector_name,
            sector_score    = sector_score,
            tickers         = tickers,
            cfg             = cfg,
            market          = "JP",
            edinet_accruals = edinet_accruals,
            all_scores_ref  = all_comp_scores,
            surprise_cache = surprise_cache,
            credit_cache   = credit_cache,
            tech_cache     = tech_cache,
            insider_cache  = insider_cache,   # ← この行を追加
            dividend_cache = dividend_cache,  # ← この行を追加
            sr_cache       = sr_cache,
            vp_cache       = vp_cache,
            edinet_quality_cache = edinet_quality_cache,
            comps_cache    = comps_cache,
            dcf_cache      = dcf_cache,
        )
        all_candidates.extend(candidates)

    # [S-3] discovery_score を全銘柄に付与
    # composite_score 上位20%の閾値を計算
    if all_comp_scores:
        all_comp_scores_sorted = sorted(all_comp_scores)
        top20_idx     = max(0, int(len(all_comp_scores_sorted) * 0.80) - 1)
        score_thresh  = all_comp_scores_sorted[top20_idx]
    else:
        score_thresh = 70.0  # フォールバック閾値

    print(f"  🔍 [S-3] discovery_score計算中（上位20%閾値: {score_thresh:.1f}点）...")

    # 業種別平均出来高を算出（業種をまたいで再計算）
    from collections import defaultdict
    sector_vols: dict[str, list] = defaultdict(list)
    for c in all_candidates:
        vol = c.get("avg_volume") or 0
        if vol > 0:
            sector_vols[c["sector_code"]].append(vol)
    sector_vol_means = {
        code: sum(vols) / len(vols)
        for code, vols in sector_vols.items()
        if vols
    }

    discovery_count = 0
    for c in all_candidates:
        sec_vol_mean = sector_vol_means.get(c["sector_code"])
        disc_score, disc_flags = _calc_discovery_score(
            data            = c,
            composite_score = c["composite_score"],
            score_threshold = score_thresh,
            sector_vol_mean = sec_vol_mean,
        )
        c["discovery_score"] = disc_score
        c["discovery_flags"] = disc_flags
        if disc_score >= 40:
            discovery_count += 1

    # 結果集計
    passed = [c for c in all_candidates if c["pass_filters"]]
    discovery_top = sorted(
        [c for c in all_candidates if c.get("discovery_score", 0) >= 40],
        key=lambda x: x.get("discovery_score", 0),
        reverse=True,
    )[:10]

    elapsed = (datetime.now() - t0).seconds
    print(
        f"  ✅ スクリーニング完了（{elapsed}秒）: "
        f"対象{len(all_candidates)}銘柄 / 通過{len(passed)}銘柄 "
        f"/ 未発見候補{discovery_count}銘柄"
    )
    if discovery_top:
        print(f"  🔍 未発見銘柄TOP3:")
        for c in discovery_top[:3]:
            flags = ", ".join(c["discovery_flags"])
            print(f"      {c['ticker']} {c['name'][:20]} "
                  f"disc={c['discovery_score']:.0f} [{flags}]")

    result = {
        "candidates":     all_candidates,
        "screened_count": len(all_candidates),
        "pass_count":     len(passed),
        "top_sectors":    top_sector_names,
        "discovery_top":  discovery_top,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_screener_report(result, cfg)
    return result


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

    mock_sectors = {
        "1016": {"composite_score": 78.0, "sector_name_ja": "電気機器"},
        "1017": {"composite_score": 65.0, "sector_name_ja": "輸送用機器"},
    }
    result = run_stock_screener(cfg, mock_sectors)
    print(f"\n通過銘柄数: {result['pass_count']}")
    print(f"未発見候補: {len(result['discovery_top'])}")
    for c in sorted(result["candidates"],
                    key=lambda x: x["composite_score"], reverse=True)[:5]:
        if c["pass_filters"]:
            print(f"  {c['ticker']} | PER:{c['per']} | ROE:{c['roe']}% "
                  f"| score:{c['composite_score']} | accrual:{c['accrual_tier']} "
                  f"| disc:{c['discovery_score']:.0f}")
