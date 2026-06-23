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

# ==============================================================
# BLOCK 0.5: セクター振り分けユーティリティ（④セクター振り分け）
# ==============================================================

def _build_jp_ticker_to_sector() -> dict:
    """
    JP_SECTOR_TICKERS の逆引きマップを生成する。
    {ticker: sector_code}
    """
    mapping = {}
    for code, tickers in JP_SECTOR_TICKERS.items():
        for t in tickers:
            mapping[t] = code
    return mapping

# 起動時に一度だけ構築
_JP_TICKER_TO_SECTOR: dict = _build_jp_ticker_to_sector()


def get_jp_sector_code(ticker: str, edinet_db: dict | None = None) -> Optional[str]:
    """
    日本株の ticker からセクターコードを返す。

    優先順:
      1. edinet_rotation_manager の meta.json の sector_code
      2. JP_SECTOR_TICKERS の逆引きマップ
      3. None（不明）

    Args:
        ticker    : "6758.T" 形式
        edinet_db : edinet_rotation_manager.load_db(cfg) の戻り値（optional）
    """
    # ① EDINET DB の meta から取得
    if edinet_db:
        meta_entry = edinet_db.get("meta", {}).get(ticker, {})
        sc = meta_entry.get("sector_code")
        if sc:
            return str(sc)

    # ② JP_SECTOR_TICKERS 逆引き
    return _JP_TICKER_TO_SECTOR.get(ticker)

# ── US株セクターマッピング ───────────────────────────────────
# yfinance の sector 文字列 → US 11セクターコード（GICS準拠）
US_GICS_SECTOR_MAP: dict[str, str] = {
    # Energy
    "Energy":                        "US_10",
    # Materials
    "Basic Materials":               "US_15",
    "Materials":                     "US_15",
    # Industrials
    "Industrials":                   "US_20",
    "Industrial Conglomerates":      "US_20",
    # Consumer Discretionary
    "Consumer Cyclical":             "US_25",
    "Consumer Discretionary":        "US_25",
    # Consumer Staples
    "Consumer Defensive":            "US_30",
    "Consumer Staples":              "US_30",
    # Health Care
    "Healthcare":                    "US_35",
    "Health Care":                   "US_35",
    # Financials
    "Financial Services":            "US_40",
    "Financials":                    "US_40",
    "Financial":                     "US_40",
    # Information Technology
    "Technology":                    "US_45",
    "Information Technology":        "US_45",
    # Communication Services
    "Communication Services":        "US_50",
    "Communication":                 "US_50",
    # Utilities
    "Utilities":                     "US_55",
    # Real Estate
    "Real Estate":                   "US_60",
}

# US_SECTOR_CODE → 日本語ラベル
US_SECTOR_LABELS: dict[str, str] = {
    "US_10": "エネルギー",
    "US_15": "素材",
    "US_20": "資本財・サービス",
    "US_25": "一般消費財・サービス",
    "US_30": "生活必需品",
    "US_35": "ヘルスケア",
    "US_40": "金融",
    "US_45": "情報技術",
    "US_50": "コミュニケーション・サービス",
    "US_55": "公益事業",
    "US_60": "不動産",
}


# Optional[str] の代わりに str | None を使う
def get_us_sector_code(ticker: str, yf_info: dict | None = None) -> tuple[str | None, str | None]:
    """
    米国株の ticker からセクターコードと日本語ラベルを返す。

    優先順:
      1. yf_info["sector"] を US_GICS_SECTOR_MAP で変換
      2. yf_info["industry"] で部分マッチ
      3. None（不明）

    Args:
        ticker  : "AAPL" 形式
        yf_info : yfinance.Ticker(ticker).info の dict（optional）

    Returns:
        (sector_code: str|None, sector_label_ja: str)
    """
    if yf_info:
        # ① sector フィールドで直接マッチ
        yf_sector = yf_info.get("sector", "")
        if yf_sector:
            code = US_GICS_SECTOR_MAP.get(yf_sector)
            if code:
                return code, (US_SECTOR_LABELS.get(code, yf_sector) or "")

        # ② industry フィールドで部分マッチ
        yf_industry = (yf_info.get("industry") or "").lower()
        for gics_name, code in US_GICS_SECTOR_MAP.items():
            if gics_name.lower() in yf_industry:
                return code, US_SECTOR_LABELS.get(code, gics_name)

    return None, "不明"


def enrich_candidates_with_sector(
    candidates: list,
    edinet_db:  dict | None = None,
) -> list:
    """
    candidates リストの各 candidate に sector_code が未設定の場合に付与する。

    JP株（".T" suffix）は get_jp_sector_code()、
    US株（".T" なし）は get_us_sector_code() を使用。

    screener の run_stock_screener() 完了後に呼び出す。

    Args:
        candidates : screener の candidate 辞書リスト
        edinet_db  : edinet_rotation_manager.load_db(cfg)（JP株用）
    """
    for c in candidates:
        if c.get("sector_code"):
            continue   # 既に付与済みならスキップ

        ticker = c.get("ticker", "")
        if ticker.endswith(".T") or ticker.endswith(".OS"):
            # JP株
            sc = get_jp_sector_code(ticker, edinet_db)
            if sc:
                c["sector_code"] = sc
                # sector_name が未設定なら補完
                if not c.get("sector_name"):
                    # cfg は参照できないので JP_SECTOR_TICKERS のコードのみ
                    c["sector_name"] = f"JP_{sc}"
        else:
            # US株（yf_info は持っていない場合は None）
            yf_info = c.get("_yf_info")   # _fetch_ticker_data が格納している場合
            sc, label = get_us_sector_code(ticker, yf_info)
            if sc:
                c["sector_code"] = sc
                if not c.get("sector_name"):
                    c["sector_name"] = label

    return candidates

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
        import yfinance as yf  # type: ignore

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
        current_price = (
            info.get("regularMarketPrice") or 
            info.get("currentPrice") or 
            info.get("previousClose") or 0
        )
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
            "current_price":  _clean(current_price),
            # [I-1] EPS成長率（加速度計算用）
            "earnings_growth":  _clean(info.get("earningsGrowth")),        # 直近年率成長率
            "earnings_qgrowth": _clean(info.get("earningsQuarterlyGrowth")), # 直近四半期成長率
            "analyst_count":  analyst_count,   # v3.0追加
            # [M-3] ネットキャッシュ計算用
            "total_cash":     _clean(info.get("totalCash")),
            "total_debt":     _clean(info.get("totalDebt")),
            "market_cap":     _clean(info.get("marketCap")),
            "payout_ratio":   _clean(info.get("payoutRatio")),
            "beta":           _clean(info.get("beta")),
            # [P-2] 事業モデル質スコア用フィールド
            "gross_margin":     _clean(info.get("grossMargins")),
            "op_margin":        _clean(info.get("operatingMargins")),
            "net_margin":       _clean(info.get("profitMargins")),
            "operating_cashflow": _clean(info.get("operatingCashflow")),
            "net_income":       _clean(info.get("netIncomeToCommon")),
            # [P-3] 長期保有候補フラグ用
            "equity_ratio":     _clean(info.get("bookValue")),      # book_value_per_share
            "debt_to_equity":   _clean(info.get("debtToEquity")),   # D/Eレシオ
            "total_assets":     _clean(info.get("totalAssets")),
            "stockholders_equity": _clean(info.get("stockholdersEquity")),
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

# ==============================================================
# BLOCK 2.4: [P-2] 事業モデル質スコア計算
# ==============================================================

def _calc_business_quality_bonus(data: dict) -> tuple[float, dict]:
    """
    片山晃モデルの「事業の質」を3軸で定量化する。

    ① 粗利率（gross_margin）
       ビジネスモデルの本質的な競争優位性を示す。
       SaaS・ブランド品・医薬品は高く、コモディティは低い。

    ② 営業CF品質（operating_cashflow / net_income）
       「利益がキャッシュに変換されているか」の確認。
       アクルーアル補正（S-2）と相互補完する。
       比率 > 1.0 = 利益の全額以上がキャッシュ化 = 高品質。

    ③ コスト構造の柔軟性（op_margin / gross_margin）
       粗利に対してどれだけ営業利益が残るか。
       高いほどSGA・固定費が低く、不況耐性が高い。

    Returns:
        (bonus: float, detail: dict)  bonus は ±20pt 以内にクランプ
    """
    bonus  = 0.0
    detail = {}

    gross_margin = data.get("gross_margin")
    op_margin    = data.get("op_margin")
    op_cf        = data.get("operating_cashflow")
    net_income   = data.get("net_income")

    # ── ① 粗利率スコア ──────────────────────────────────────
    if gross_margin is not None and gross_margin > 0:
        if   gross_margin >= 0.50: gm_bonus = 20.0
        elif gross_margin >= 0.35: gm_bonus = 12.0
        elif gross_margin >= 0.20: gm_bonus =  5.0
        elif gross_margin >= 0.10: gm_bonus =  0.0
        else:                      gm_bonus = -5.0
        bonus += gm_bonus
        detail["gross_margin"] = round(gross_margin * 100, 1)
        detail["gm_bonus"]     = gm_bonus

    # ── ② 営業CF品質（CF/純利益比率）────────────────────────
    if op_cf is not None and net_income is not None and abs(net_income) > 1e6:
        cf_ratio = op_cf / net_income
        if   cf_ratio >= 1.50: cf_bonus = 20.0   # 利益の1.5倍のCF = 最高品質
        elif cf_ratio >= 1.10: cf_bonus = 12.0
        elif cf_ratio >= 0.80: cf_bonus =  5.0
        elif cf_ratio >= 0.30: cf_bonus =  0.0
        elif cf_ratio <  0:    cf_bonus = -15.0  # CF がマイナス = 危険
        else:                  cf_bonus = -10.0  # CF が利益の30%未満
        bonus += cf_bonus
        detail["cf_ratio"] = round(cf_ratio, 2)
        detail["cf_bonus"] = cf_bonus
    elif op_cf is not None and op_cf < 0:
        # 純利益がゼロ/マイナスでもCFがマイナスなら明確にペナルティ
        bonus += -10.0
        detail["cf_bonus"] = -10.0

    # ── ③ コスト構造の柔軟性（op_margin / gross_margin）──────
    if (gross_margin is not None and op_margin is not None
            and gross_margin > 0.05):  # 粗利がほぼゼロの業種は除外
        cost_ratio = op_margin / gross_margin
        if   cost_ratio >= 0.60: cost_bonus = 10.0
        elif cost_ratio >= 0.40: cost_bonus =  5.0
        elif cost_ratio >= 0.20: cost_bonus =  0.0
        elif cost_ratio >= 0.05: cost_bonus = -3.0
        else:                    cost_bonus = -5.0
        bonus += cost_bonus
        detail["cost_ratio"] = round(cost_ratio, 2)
        detail["cost_bonus"] = cost_bonus

    # クランプ: ±20pt 以内
    bonus = max(-20.0, min(20.0, bonus))
    detail["total_bonus"] = round(bonus, 1)

    return round(bonus, 1), detail

# ==============================================================
# BLOCK 2.6: [GAP-4] 戦略別ラベル付与
# ==============================================================

def _assign_strategy_label(candidate: dict, cfg=None) -> str:
    """
    [GAP-4] candidate の属性から戦略ラベルを判定する。

    優先順位（複数該当時は上位を採用）:
      satellite_event     : イベントドリブン（決算サプライズ高確率 / インサイダー買い）
      satellite_smallcap  : 小型株・特殊状況（アクティビスト候補 / ネットキャッシュリッチ）
      satellite_info      : 情報コスト優位（未発見銘柄 / ArXivシグナル）
      core                : コア戦略（それ以外のTOP_TIER通過銘柄）

    Returns:
        "core" / "satellite_event" / "satellite_smallcap" / "satellite_info"
    """
    # ── サテライト1: イベントドリブン ──────────────────────────
    is_high_prob = candidate.get("is_high_prob_surprise", False)
    is_insider   = (candidate.get("credit_flag", "") or "").startswith("insider")
    consv        = candidate.get("conservatism_score", 0) or 0
    acc          = candidate.get("acc_bonus", 0) or 0
    if is_high_prob or (consv >= 70 and acc >= 7):
        return "satellite_event"

    # ── サテライト2: 小型株・特殊状況 ──────────────────────────
    is_activist     = candidate.get("is_activist_target", False)
    is_net_cash     = candidate.get("is_net_cash_rich", False)
    activist_score  = candidate.get("activist_score", 0) or 0
    mcap            = candidate.get("market_cap_b", 999) or 999
    if is_activist or is_net_cash or (activist_score >= 70 and mcap <= 500):
        return "satellite_smallcap"

    # ── サテライト3: 情報コスト優位 ─────────────────────────────
    discovery   = candidate.get("discovery_score", 0) or 0
    analyst_cnt = candidate.get("analyst_count")
    if discovery >= 60 or (analyst_cnt is not None and analyst_cnt <= 2):
        return "satellite_info"

    # ── コア ────────────────────────────────────────────────────
    return "core"

# ==============================================================
# BLOCK 2.45: [P-3] 長期保有候補フラグ計算
# ==============================================================

def _calc_longterm_hold_flag(
    data: dict,
    edinet_data: dict | None = None,
) -> tuple[bool, float, list]:
    """
    片山晃モデルの「10年持てる銘柄」基準を数値化する。

    条件（AND 条件）:
      ① 自己資本比率 >= 60%（財務の堅牢性）
      ② EPS成長率（直近年率）> 0（増益基調）
      ③ ROE >= 10%（資本効率が高い）
      ④ アクルーアルtier != "C"（利益の質が担保されている）

    EDINET の連続増益データがあれば③を3期連続増益で補強する。

    Returns:
        (is_longterm_hold: bool, confidence: float, flags: list[str])
        confidence: 0.0〜1.0（条件充足率）
    """
    flags      = []
    conditions = []

    # ── ① 自己資本比率 >= 60% ───────────────────────────────
    equity_ratio = None
    se = data.get("stockholders_equity")
    ta = data.get("total_assets")
    if se is not None and ta is not None and ta > 0:
        equity_ratio = se / ta
    elif data.get("debt_to_equity") is not None:
        de = data["debt_to_equity"]
        if de >= 0:
            equity_ratio = 1.0 / (1.0 + de / 100.0) if de > 0 else 1.0

    if equity_ratio is not None:
        if equity_ratio >= 0.60:
            flags.append(f"自己資本比率{equity_ratio*100:.0f}%")
            conditions.append(True)
        else:
            conditions.append(False)
    else:
        conditions.append(None)  # データなし = 評価保留

    # ── ② EPS 増益基調（earnings_growth > 0）──────────────
    eg = data.get("earnings_growth")
    if eg is not None:
        if eg > 0:
            flags.append(f"増益({eg*100:+.1f}%)")
            conditions.append(True)
        else:
            conditions.append(False)
    else:
        conditions.append(None)

    # ── ③ ROE >= 10% ────────────────────────────────────────
    roe = data.get("roe")
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) < 1 else roe
        if roe_pct >= 10.0:
            flags.append(f"ROE{roe_pct:.1f}%")
            conditions.append(True)
        else:
            conditions.append(False)
    else:
        conditions.append(None)

    # ── ④ アクルーアル tier != "C" ──────────────────────────
    # accrual_tier は _calc_scores() で付与されるが、
    # _fetch_ticker_data() 時点ではまだ計算されていない。
    # edinet_data 経由でアクルーアルが取得できる場合のみ評価する。
    accrual_ok = True   # デフォルトは OK（データなしは不利にしない）
    if isinstance(edinet_data, dict):
        analysis = edinet_data.get("analysis", {})
        acr = analysis.get("accrual_ratio")
        if acr is not None and float(acr) >= 0.05:
            accrual_ok = False   # tier C 相当
            conditions.append(False)
        else:
            conditions.append(True)
    else:
        conditions.append(None)

    # ── EDINET 連続増益（3期以上）の補強チェック ─────────────
    if isinstance(edinet_data, dict):
        fin = edinet_data.get("financials", {})
        op_income = fin.get("operating_income", [])
        if len(op_income) >= 3:
            # 直近3期が全て増益かチェック
            recent3 = [v for v in op_income[-3:] if v is not None]
            if len(recent3) >= 3:
                consecutive = all(
                    recent3[i] > recent3[i-1]
                    for i in range(1, len(recent3))
                )
                if consecutive:
                    flags.append("3期連続増益✅")

    # ── 判定 ─────────────────────────────────────────────────
    valid = [c for c in conditions if c is not None]
    true_count = sum(1 for c in valid if c)
    confidence = true_count / len(valid) if valid else 0.0

    # 全条件が True（データなし項目は除外して判定）かつ
    # 有効条件が3件以上ある場合に長期保有候補フラグを立てる
    is_longterm = (
        len(valid) >= 3
        and all(c for c in valid)
        and confidence >= 1.0
    )

    if is_longterm:
        flags.append("🌱長期保有候補")

    return is_longterm, round(confidence, 2), flags

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

def _apply_capped_adjustment(base_score: float, delta: float,
                             cap: float, label: str = "") -> tuple[float, float]:
    """
    補正量に上限を適用し、クランプ済みのスコアと実際の補正量を返す。

    Args:
        base_score : 補正前スコア
        delta      : 補正量（正=追い風 / 負=逆風）
        cap        : 補正量の絶対値上限（例: 10.0）
        label      : デバッグ用ラベル

    Returns:
        (new_score: float, actual_delta: float)
    """
    # cap 適用
    capped_delta = max(-cap, min(cap, delta))
    if capped_delta != delta and label:
        print(f"    ⚠️  [{label}] 補正上限適用: "
              f"{delta:+.1f}pt → {capped_delta:+.1f}pt（上限±{cap}pt）")
    new_score = round(max(-30.0, min(100.0, base_score + capped_delta)), 1)
    return new_score, capped_delta

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

# ==============================================================
# BLOCK 2.3: Wave 2 FE関数（FE-1 / FE-2）
# ==============================================================

def _calc_risk_adjusted_score(
    ticker:          str,
    composite_score: float,
    data:            dict,
    cfg=None,
) -> float:
    """
    FE-1: ATR基準リスク調整スコアを算出する。

    yfinance の hist データが利用不可の場合は beta をボラティリティ代理変数として使用。
    risk\\_adjusted\\_score = composite \\div PENALTY\\_HIGH\\_VOL (高ボラ時)
    risk\\_adjusted\\_score = composite \\Times PENALTY\\_LOW\\_VOL (低ボラ/低流動性時)

    Args:
        ticker:          銘柄コード（ログ用）
        composite_score: 元のcomposite_score
        data:            _fetch_ticker_data() 返り値辞書
        cfg:             config モジュール（None 可）

    Returns:
        float: risk_adjusted_score (0.0〜100.0)
        ※ 失敗時は composite_score をそのまま返す
    """
    try:
        penalty_high = float(getattr(cfg, "RISK_ADJ_PENALTY_HIGH_VOL", 1.20))
        penalty_low  = float(getattr(cfg, "RISK_ADJ_PENALTY_LOW_VOL",  0.85))

        beta = data.get("beta")
        if beta is None:
            # beta 不明 → 調整なし
            return round(float(composite_score), 1)

        b = float(beta)
        # 異常値除去
        if not (-2.0 <= b <= 5.0):
            return round(float(composite_score), 1)

        if b > 1.5:
            # 高ボラティリティ銘柄: リスク割引
            adjusted = composite_score / penalty_high
        elif 0.0 < b < 0.5:
            # 低ボラティリティ銘柄: 流動性リスクを考慮した軽微な割引
            adjusted = composite_score * penalty_low
        else:
            # 通常範囲: 調整なし
            adjusted = composite_score

        return round(min(100.0, max(0.0, adjusted)), 1)

    except Exception:
        return round(float(composite_score), 1)


def _calc_revision_score(
    ticker: str,
    data:   dict,
    cfg=None,
) -> float:
    """
    FE-2: アナリスト修正スコアを算出する（JP版・上限+3pt）。

    直近30日のupgrade/downgrade集計が理想だが、
    yfinance の info から取得できる rec_mean を主要代理変数として使用する。
    revision\\_score \\in [-0.5, 0.5]（quality\\_score加算前のスコア）
    加算値 = revision\\_score \times 6.0（JPは上限+3pt / 下限-3pt）

    Returns:
        float: quality_score への加算点（-3.0〜+3.0）
    """
    try:
        window = int(getattr(cfg, "ANALYST_REVISION_WINDOW_DAYS", 30))
        # ── rec_mean（アナリスト推奨平均）を revision proxy として使用 ──
        # yfinance: 1.0=Strong Buy, 2.0=Buy, 3.0=Hold, 4.0=Sell, 5.0=Strong Sell
        rec_mean = data.get("rec_mean")
        if rec_mean is None:
            return 0.0

        r = float(rec_mean)
        if r <= 1.5:
            raw = 0.50   # Strong Buy
        elif r <= 2.0:
            raw = 0.30   # Buy
        elif r <= 2.5:
            raw = 0.10   # 弱いBuy
        elif r <= 3.0:
            raw = 0.00   # Hold
        elif r <= 3.5:
            raw = -0.20  # 弱いSell
        else:
            raw = -0.50  # Sell / Strong Sell

        # JP: 上限 +3pt / 下限 -3pt
        jp_max = float(getattr(cfg, "PEAD_TRIGGER_MIN_REVISION", 0.3)) * 10.0
        jp_max = min(jp_max, 3.0)
        return round(min(jp_max, max(-jp_max, raw * 6.0)), 2)

    except Exception:
        return 0.0


def _check_pead_trigger(
    ticker:         str,
    revision_score: float,
    cfg=None,
) -> bool:
    """
    FE-2: PEAD（Post-Earnings Announcement Drift）トリガー判定。

    revision_score が PEAD_TRIGGER_MIN_REVISION 以上の場合に True を返す。
    alert_notifier.py のトリガー⑳で参照される。

    Returns:
        bool: True → PEAD シグナル発動
    """
    try:
        min_rev = float(getattr(cfg, "PEAD_TRIGGER_MIN_REVISION", 0.3))
        return float(revision_score) >= min_rev
    except Exception:
        return False

# ==============================================================
# BLOCK 2.8: Wave 3 FE-7 カタリストボーナス算出
# ==============================================================

def _calc_catalyst_bonus(
    ticker:      str,
    sector_code: str,
    cat_data:    dict,
    cfg=None,
) -> float:
    """
    FE-7: カタリストカレンダーからボーナス（最大 +5pt）を算出する。

    cat_data は _screen_sector() のループ外で一括ロードした
    catalyst_calendar.json の内容を渡す。

    Returns:
        float: 0.0〜5.0
    """
    try:
        if not cat_data:
            return 0.0
        from catalyst_calendar import calc_catalyst_bonus as _cb
        return _cb(ticker, sector_code, cat_data, cfg)
    except Exception:
        return 0.0

def _calc_scores(
    data:                dict,
    sector_score:        float,
    edinet_accrual:      Optional[float] = None,
    sector_accrual_mean: Optional[float] = None,
    surprise_bonus:      float = 0.0,
    bq_bonus:            float = 0.0,   # ★ P-2追加（事業モデル質ボーナス）
    ceo_bonus:           float = 0.0,   # ★ I-3追加（経営者スコアボーナス）
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
    # ── [Wave 2 - BM Ph2] 業態別 value_score 正規化 ──────────────
    try:
        from business_model_normalizer import (
            detect_bm_norm_type as _det_bm,
            normalize_value_score_by_bm as _norm_vs,
        )
        _sector_code = str(data.get("sector_code", ""))
        _bm_norm_type = _det_bm(_sector_code, market="JP")
        value_score = _norm_vs(value_score, _bm_norm_type, data)
    except Exception:
        _bm_norm_type = "NORM_STANDARD"
    inv_bonus = 0.0   # if roe ブロック外で初期化（ROEなし銘柄でも参照できるよう）
    # [I-1] EPS加速度スコア計算
    # earnings_growth（直近年率）と earnings_qgrowth（直近四半期）の差から加速度を算出
    acc_bonus = 0.0
    eg_annual  = data.get("earnings_growth")    # 例: 0.25 = 25%
    eg_quarter = data.get("earnings_qgrowth")   # 例: 0.40 = 40%（四半期）
    if eg_annual is not None and eg_quarter is not None:
        # 四半期成長率（年換算）と年率成長率の差 = 加速度の近似
        # 四半期成長率を年換算: (1+q)^4 - 1 の近似として q*4 を使用
        eg_quarter_annualized = eg_quarter * 4
        acceleration = eg_quarter_annualized - eg_annual
        # 加速度をボーナスに変換（±10pt を上限）
        if acceleration > 0.30:    acc_bonus = 10.0   # 強加速（年換算30%以上上振れ）
        elif acceleration > 0.15:  acc_bonus =  7.0
        elif acceleration > 0.05:  acc_bonus =  4.0
        elif acceleration < -0.30: acc_bonus = -10.0  # 強減速
        elif acceleration < -0.15: acc_bonus =  -7.0
        elif acceleration < -0.05: acc_bonus =  -4.0
    elif eg_quarter is not None and eg_quarter > 0.30:
        # 年率データなしでも四半期成長率が30%超なら加速シグナルとして扱う
        acc_bonus = 5.0
    # クォリティスコア base (0-100): ROEベース
    if roe is not None:
        quality_base = max(0, min(100, roe / 0.30 * 100))
    else:
        quality_base = 50.0
    # [A-7] 決算サプライズボーナス（+5 〜 -5pt）
    # 在庫回転率ボーナス（製造・小売に有意 / 金融・サービスはN/A）
    inv_turn = data.get("inventory_turnover")
    if inv_turn is not None:
        if   inv_turn >= 12: inv_bonus =  4.0  # 月1回転以上 = 優良
        elif inv_turn >= 6:  inv_bonus =  2.0  # 2ヶ月に1回転 = 良好
        elif inv_turn <= 2:  inv_bonus = -3.0  # 半年に1回転 = 過剰在庫
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

    # ── [Wave 2 - BM Ph2] bm_bonus 算出 ─────────────────────────
    _bm_bonus = 0.0
    try:
        from business_model_normalizer import calc_bm_bonus as _calc_bm_bonus
        _bm_bonus = _calc_bm_bonus(
            ticker=str(data.get("symbol", data.get("ticker", ""))),
            data=data,
            sector_code=str(data.get("sector_code", "")),
            market="JP",
        )
    except Exception:
        _bm_bonus = 0.0

    # ── [Wave 2 - FE-2] revision_score 算出 ──────────────────────
    _revision_score = 0.0
    try:
        _revision_score = _calc_revision_score(
            ticker=str(data.get("symbol", data.get("ticker", ""))),
            data=data,
            cfg=None,
        )
    except Exception:
        _revision_score = 0.0

    quality_score = round(
        max(0, min(100,
            quality_base + acr_bonus + relative_bonus
            + surprise_bonus + inv_bonus + acc_bonus
            + bq_bonus  + ceo_bonus          # ★ P-2追加
            + _bm_bonus + _revision_score    # ★ Wave 2追加
        )),
        1,
    )

# ── [Pearl Ph1] 光る原石スコアを計算し quality_score に加算 ──
    pearl_score = 0.0
    pearl_bonus = 0.0
    pearl_tier  = None
    try:
        from pearl_score_calculator import calc_pearl_score as _calc_pearl
        import config as _cfg_pearl
        if getattr(_cfg_pearl, "ENABLE_PEARL_SCORE", True):
            _ticker = data.get("symbol", data.get("ticker", ""))
            # EPS加速度を data に補完（pearl D1 計算用）
            # ── Wave 7: D3統合用フィールドを data に補完 ────────
            # pearl_score_calculator.py の _calc_d3_earnings_quality() が
            # edinet_segment_text / catalyst_score / midterm_overall_score /
            # midterm_trajectory を参照する。
            try:
                import json as _j7
                from pathlib import Path as _p7
                _cache_dir_ps = _p7(
                    getattr(__import__("config"), "CACHE_DIR", "stock_data/cache")
                )
                _ticker_ps = data.get("symbol", data.get("ticker", ""))
                _code_ps   = str(_ticker_ps).split(".")[0]

                # (a) EDINET ローテーションキャッシュからセグメントテキストを取得
                if "edinet_segment_text" not in data:
                    _rot_p = _cache_dir_ps / "edinet_rotation_cache.json"
                    if _rot_p.exists():
                        _rot = _j7.loads(_rot_p.read_text(encoding="utf-8"))
                        _seg_texts = _rot.get("companies", {}).get(
                            _code_ps, {}
                        ).get("texts", [])
                        data["edinet_segment_text"] = (
                            " ".join(str(t) for t in _seg_texts)
                            if isinstance(_seg_texts, list)
                            else str(_seg_texts)
                        )

                # (b) text_signal_cache からカタリストスコアを取得
                if "catalyst_score" not in data:
                    _ts_p = _cache_dir_ps / "text_signal_cache.json"
                    if _ts_p.exists():
                        _ts = _j7.loads(_ts_p.read_text(encoding="utf-8"))
                        _cat_key = f"{_code_ps}_catalyst"
                        _cat_e   = _ts.get(_cat_key, {})
                        data["catalyst_score"] = float(
                            _cat_e.get("catalyst_score", 0.0)
                        )

                # (c) midterm_plan_cache からスコア・軌道を取得
                if "midterm_overall_score" not in data:
                    _mt_p = _cache_dir_ps / "midterm_plan_cache.json"
                    if _mt_p.exists():
                        _mt = _j7.loads(_mt_p.read_text(encoding="utf-8"))
                        _mt_e = _mt.get("plans", {}).get(_ticker_ps, {})
                        data["midterm_overall_score"] = float(
                            _mt_e.get("overall_score", 50.0)
                        )
                        data["midterm_trajectory"] = _mt_e.get(
                            "trajectory", "on_track"
                        )
            except Exception:
                pass
            if eg_annual is not None and eg_quarter is not None:
                data["earnings_acceleration"] = round(
                    float(eg_quarter) * 4 - float(eg_annual), 3
                )
            _pr = _calc_pearl(data, _cfg_pearl, market="JP", ticker=_ticker)
            pearl_score   = _pr.get("pearl_score", 0.0)
            pearl_bonus   = _pr.get("pearl_bonus", 0.0)
            pearl_tier    = _pr.get("pearl_tier")
            # quality_score に pearl_bonus を加算（上限100pt）
            quality_score = round(min(100.0, quality_score + pearl_bonus), 1)
    except Exception:
        pass

    # ── [Wave 2 - Size Ph2] size_tier取得 + tier別composite算出 ──
    _size_tier = "Mid"   # デフォルト
    _size_weights = {"sector_score": 0.40, "value_score": 0.30, "quality_score": 0.30}
    try:
        from size_decay_manager import resolve_size_tier as _res_tier
        from size_decay_manager import calc_tier_composite_score as _tier_comp
        _ticker_key = str(data.get("symbol", data.get("ticker", "")))
        _mcap_b     = data.get("market_cap_b")
        _mcap_raw   = data.get("market_cap")
        _size_tier  = _res_tier(
            ticker        = _ticker_key,
            market_cap_raw= _mcap_raw,
            market_cap_b  = _mcap_b,
            market        = "JP",
        )
        composite, _size_weights = _tier_comp(
            sector_score, value_score, quality_score, _size_tier
        )
    except Exception:
        # フォールバック: 固定ウェイト（既存ロジックと同じ）
        composite = round(
            sector_score  * 0.40
            + value_score  * 0.30
            + quality_score * 0.30,
            1,
        )

    # ── [Wave 2 - FE-1] risk_adjusted_score 算出 ─────────────────
    _risk_adj_score = composite   # デフォルト: composite と同値
    try:
        _risk_adj_score = _calc_risk_adjusted_score(
            ticker=str(data.get("symbol", data.get("ticker", ""))),
            composite_score=composite,
            data=data,
        )
    except Exception:
        _risk_adj_score = composite

    return {
        "sector_score":       round(sector_score, 1),
        "value_score":        value_score,
        "quality_score":      quality_score,
        "composite_score":    composite,
        "accrual_ratio":      edinet_accrual,
        "accrual_tier":       acr_tier,
        "accrual_bonus":      round(acr_bonus + relative_bonus, 1),
        "surprise_bonus":     surprise_bonus,
        "acc_bonus":          round(acc_bonus, 1),
        "earnings_acceleration": round(eg_quarter * 4 - eg_annual, 3)
                                 if (eg_annual is not None and eg_quarter is not None)
                                 else None,
        "bq_bonus":           round(bq_bonus, 1),
        "pearl_score":        round(pearl_score, 1),
        "pearl_bonus":        round(pearl_bonus, 1),
        "pearl_tier":         pearl_tier,
        # ── Wave 2 追加フィールド ──────────────────────────────
        "size_tier":          _size_tier,              # Wave 2 - Size Ph2
        # ── Wave 7 追加フィールド ──────────────────────────────
        "edinet_segment_text":  data.get("edinet_segment_text", ""),
        "catalyst_score":       float(data.get("catalyst_score", 0.0)),
        "midterm_overall_score": float(data.get("midterm_overall_score", 0.0)),
        "midterm_trajectory":   data.get("midterm_trajectory", "on_track"),
        "size_weights":       _size_weights,           # Wave 2 - Size Ph2
        "bm_bonus":           round(_bm_bonus, 2),    # Wave 2 - BM Ph2
        "bm_norm_type":       _bm_norm_type,           # Wave 2 - BM Ph2
        "revision_score":     round(_revision_score, 2), # Wave 2 - FE-2
        "risk_adjusted_score": _risk_adj_score,        # Wave 2 - FE-1
    }

# ==============================================================
# BLOCK 2.5: [M-3] アクティビスト候補スコア計算
# ==============================================================

def _calc_activist_score(data: dict, edinet_data: dict | None = None) -> tuple[float, list]:
    """
    村上世彰モデルの「放置された割安資産」を定量化する。

    スコア構成（合計100点満点）:
      PBR < 1.0                    → +25pt（解散価値割れ）
      ROE < 8%（改善余地あり）     → +15pt（資本効率の低さ）
      ネットキャッシュ/時価総額>20% → +25pt（現金山積み）
      配当性向 < 30%（還元余地）   → +20pt
      親会社持株 > 50%             → +15pt（解放余地あり）

    Returns:
        (activist_score: float, flags: list[str])
    """
    score = 0.0
    flags = []

    pbr        = data.get("pbr")
    roe        = data.get("roe")        # 小数表記（0.08=8%）
    total_cash = data.get("total_cash") # 円
    total_debt = data.get("total_debt") # 円
    market_cap = data.get("market_cap") # 円（raw）
    payout     = data.get("payout_ratio")

    # ── ① PBR < 1.0（解散価値割れ）────────────────────────
    if pbr is not None and pbr > 0:
        if pbr < 1.0:
            score += 25.0
            flags.append(f"PBR割れ({pbr:.2f}x)")
        elif pbr < 1.5:
            score += 10.0
            flags.append(f"PBR低({pbr:.2f}x)")

    # ── ② ROE < 8%（改善余地あり）─────────────────────────
    if roe is not None:
        roe_pct = roe * 100 if abs(roe) < 1 else roe  # 0.08 → 8%
        if roe_pct < 8.0 and roe_pct > -5.0:   # 赤字は除外
            score += 15.0
            flags.append(f"低ROE({roe_pct:.1f}%)")
        elif roe_pct < 12.0:
            score += 7.0
            flags.append(f"中ROE({roe_pct:.1f}%)")

    # ── ③ ネットキャッシュ/時価総額 > 20% ─────────────────
    if total_cash is not None and market_cap is not None and market_cap > 0:
        net_cash = total_cash - (total_debt or 0.0)
        net_cash_ratio = net_cash / market_cap
        if net_cash_ratio > 0.30:
            score += 25.0
            flags.append(f"高ネットキャッシュ({net_cash_ratio*100:.0f}%)")
        elif net_cash_ratio > 0.20:
            score += 15.0
            flags.append(f"中ネットキャッシュ({net_cash_ratio*100:.0f}%)")
        elif net_cash_ratio > 0.10:
            score += 5.0
            flags.append(f"ネットキャッシュあり({net_cash_ratio*100:.0f}%)")

    # ── ④ 配当性向 < 30%（還元余地大）────────────────────
    if payout is not None and payout >= 0:
        if payout < 0.30:
            score += 20.0
            flags.append(f"低配当性向({payout*100:.0f}%→還元余地)")
        elif payout < 0.50:
            score += 8.0
            flags.append(f"配当性向中({payout*100:.0f}%)")

    # ── ⑤ EDINET: 親会社持株 > 50%（解放余地）─────────────
    # edinet_rotation_manager の大株主情報から取得（将来実装）
    # 現在は edinet_data 経由で近似値を使用
    if isinstance(edinet_data, dict):
        analysis = edinet_data.get("analysis", {})
        parent_ownership = analysis.get("parent_ownership_pct")
        if parent_ownership is not None:
            if parent_ownership > 50.0:
                score += 15.0
                flags.append(f"親会社持株{parent_ownership:.0f}%")
            elif parent_ownership > 30.0:
                score += 7.0
                flags.append(f"持株{parent_ownership:.0f}%")

    # ── 触媒待ち割安フラグ ───────────────────────────────────
    # スコア70以上 = 4〜5条件が複合 = 「触媒があれば動く」銘柄
    is_activist_target = score >= 70.0
    if is_activist_target:
        flags.append("🎯触媒待ち割安")

    return round(score, 1), flags

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
                   sector_tier: str = "TOP",   # ★ GAP-6追加 "TOP"/"MID"/"BOT"
                   surprise_cache: dict | None = None,
                   edinet_accruals: dict | None = None,
                   all_scores_ref: list | None = None,
                   credit_cache: dict | None = None,
                   tech_cache: dict | None = None,
                   insider_cache: dict | None = None,
                   dividend_cache: dict | None = None,
                   sr_cache: dict | None = None,
                   vp_cache: dict | None = None,
                   edinet_quality_cache: dict | None = None,
                   comps_cache: dict | None = None,
                   dcf_cache: dict | None = None
                   ) -> list:
    """
    1 業種の銘柄リストをスクリーニングする。
    """
    accruals    = edinet_accruals or {}
    candidates  = []

    # 業種平均アクルーアル（S-2: 相対評価用）
    sector_accrual_mean = _calc_sector_accrual_mean(tickers, accruals)

    # ── [GAP-6] BOT_TIER はスキップ（GAP-2で除外済みだが念のため） ──
    if sector_tier == "BOT":
        print(f"    [{sector_code}] {sector_name}: BOT_TIER → スキップ")
        return []

    # ── [GAP-6] MID_TIER は軽量評価パス ──────────────────────────
    # MIDは前日のキャッシュスコアを維持し、急変した銘柄のみ再評価する
    _is_mid_tier = (sector_tier == "MID")
    if _is_mid_tier:
        print(f"    [{sector_code}] {sector_name}: MID_TIER → 軽量評価モード")

    # 1. 業種内全銘柄の基礎データを収集
    raw_data_list = []
    print(f"    [{sector_code}] {sector_name}: {len(tickers)}銘柄チェック中...")

    for ticker in tickers:
        data = _fetch_ticker_data(ticker)
        if data is not None:
            raw_data_list.append((ticker, data))
        time.sleep(0.2)

    # 業種平均出来高算出（S-3: 未発見スコア用）
    vols = [d.get("avg_volume", 0) for (_, d) in raw_data_list if d.get("avg_volume")]
    sector_vol_mean = sum(vols) / len(vols) if vols else None

    # 2. 決算サプライズ用モジュールの動的読み込み（ループ外で1回のみ）
    # [P-2][I-3] ループ初回の未束縛エラー防止（各ループ内で再計算される）
    bq_bonus  = 0.0
    bq_detail = {}
    ceo_bonus = 0.0
    # [Wave 3 - FE-7] カタリストカレンダーをループ外で一括読み込み
    _cat_data_jp: dict = {}
    try:
        from catalyst_calendar import load_catalyst_calendar as _load_cat
        _cat_data_jp = _load_cat(cfg) or {}
    except Exception:
        pass

    # 3. メインループ：フィルタリングとスコアリング
    for ticker, data in raw_data_list:
        # ── [GAP-6] MID_TIER: 急変した銘柄のみ全評価、安定銘柄はスコア維持 ──
        if _is_mid_tier:
            price_now = data.get("current_price")
            # yfinanceから直近の価格変化率を簡易チェック（±3%超で再評価）
            # ここでは _fetch_ticker_data の返り値から直近価格変化を取得する
            # 実装: 前日キャッシュとの差分 → screener_historyを参照
            # フォールバック: MID_TIERは評価フラグを通常通り行うが
            #   edinet・comps・DCFなどの重いキャッシュ参照をスキップする
            _mid_light = True   # 軽量フラグ（重いキャッシュをスキップ）
        else:
            _mid_light = False

        # A. フィルター判定（1回のみ実行）
        passed, detail = _apply_filters(data, cfg)
        
        # B. 各種ボーナスの集計
        total_bonus = 0.0
        cr_label = ""
        
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
                _cd_mod = _imp.import_module("credit_data")  # type: ignore
                cr_bonus, cr_label = _cd_mod.get_credit_bonus(ticker, credit_cache)
                total_bonus += cr_bonus
            except Exception:
                pass
        # [NEW] EDINET品質ボーナス
        eq_bonus = 0.0
        # MID_TIER は重いキャッシュ参照をスキップ（APIコール節約）
        if not _mid_light and edinet_quality_cache and ticker in edinet_quality_cache:
            try:
                _sfl_mod = _imp.import_module("screener_feedback_loop")  # type: ignore
                eq_bonus = _sfl_mod.get_edinet_bonus(ticker, edinet_quality_cache)
            except Exception:
                pass

        # インサイダーボーナス
        if insider_cache:
            try:
                _im_mod = _imp.import_module("insider_monitor")  # type: ignore
                total_bonus += _im_mod.get_insider_bonus(ticker, insider_cache)
            except Exception:
                pass

        # 配当ボーナス
        if dividend_cache:
            try:
                _dc_mod = _imp.import_module("dividend_calendar")  # type: ignore
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
            surprise_bonus      = total_bonus,
            bq_bonus            = bq_bonus,    # ★ P-2追加  # type: ignore
            ceo_bonus           = ceo_bonus,     # ★ I-3追加  # type: ignore
        )
        # [5-2] comps相対バリューボーナス（value_scoreのブレンド）
        if not _mid_light and comps_cache and ticker in comps_cache:
            try:
                import sys as _sys, importlib as _imp
                _sys.path.insert(0, str(Path(__file__).parent))
                _ca_mod = _imp.import_module("comps_analyzer")  # type: ignore
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
        if not _mid_light and dcf_cache and ticker in dcf_cache:
            try:
                _dcf_mod = _imp.import_module("dcf_valuation")  # type: ignore
                db = _dcf_mod.get_dcf_bonus(ticker, dcf_cache)
                if db != 0:
                    scores["dcf_bonus"]   = db
                    scores["value_score"] = round(
                        max(0, min(100, scores.get("value_score", 50) + db)), 1)
                    scores["composite_score"] = round(
                        scores.get("sector_score", 50)    * 0.35
                        + scores["value_score"]           * 0.25
                        + scores.get("quality_score", 50) * 0.20
                        + scores.get("technical_score", 50) * 0.20, 1)
            except Exception:
                pass
        # [M-3] アクティビスト候補スコア計算
        activist_score, activist_flags = _calc_activist_score(data)

        # [P-2] 事業モデル質ボーナス計算
        bq_bonus, bq_detail = _calc_business_quality_bonus(data)

        # [I-3] CEO スコアをキャッシュから取得（ceo_evaluator は screener 後に実行）
        # ここでは前回実行時の ceo_scores.json を参照する
        ceo_score_cached = 0.0
        ceo_bonus        = 0.0
        try:
            import sys as _sys, importlib as _imp
            _sys.path.insert(0, str(Path(__file__).parent))
            _ce = _imp.import_module("ceo_evaluator")
            _ceo_db = _ce.load_ceo_db(cfg)
            _ceo_latest = _ceo_db.get(ticker, {}).get("latest", {})
            ceo_score_cached = _ceo_latest.get("ceo_score", 0.0) or 0.0
            ceo_bonus        = _ceo_latest.get("quality_bonus", 0.0) or 0.0
        except Exception:
            pass
        # [P-3] 長期保有候補フラグ計算
        is_longterm, lt_confidence, lt_flags = _calc_longterm_hold_flag(data)
        
        # [4-6] SR ボーナスを technical_score に加算
        if sr_cache and ticker in sr_cache:
            try:
                _sr_mod = _imp.import_module("support_resistance")  # type: ignore
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
                _vp_mod = _imp.import_module("volume_profile")  # type: ignore
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
                _ts_mod = _imp.import_module("technical_scorer")  # type: ignore
                scores = _ts_mod.integrate_technical_score(
                    {**data, **scores}, tech_cache[ticker]
                )
            except Exception:
                pass

        # ── [Wave 3 - FE-7] Catalyst Bonus 計算 ─────────────────
        _catalyst_bonus = 0.0
        try:
            _catalyst_bonus = _calc_catalyst_bonus(
                ticker, sector_code, _cat_data_jp, cfg
            )
            # composite_score に catalyst_bonus を加算（上限100pt）
            if _catalyst_bonus > 0 and "composite_score" in scores:
                scores["composite_score"] = round(
                    min(100.0, scores["composite_score"] + _catalyst_bonus), 1
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
            # ── Wave 2 追加フィールド ─────────────────────────
            "size_tier":          scores.get("size_tier", "Mid"),
            "risk_adjusted_score": scores.get("risk_adjusted_score",
                                              scores.get("composite_score", 0.0)),
            "revision_score":     scores.get("revision_score", 0.0),
            "bm_bonus":           scores.get("bm_bonus", 0.0),
            "bm_norm_type":       scores.get("bm_norm_type", "NORM_STANDARD"),
            "credit_flag":    cr_label,
            "inventory_turnover": data.get("inventory_turnover"),
            "inv_bonus":      scores.get("inv_bonus", 0.0), # scoresから取得するか、別途計算するか仕様に合わせて調整
            "sr_bonus":  scores.get("sr_bonus", 0.0),
            "vp_bonus":     scores.get("vp_bonus", 0.0),
            "edinet_bonus": eq_bonus,
            "current_price": data.get("current_price"),
            "comps_bonus": scores.get("comps_bonus", 0.0),
            "dcf_bonus": scores.get("dcf_bonus", 0.0),
            # ── Wave 3 追加フィールド ──────────────────────────────
            "catalyst_bonus":     _catalyst_bonus,    # Wave 3 - FE-7
            "independence_score": 0.0,                # Wave 3 - Independence Ph1（後段で更新）
            "is_latent_pearl":    False,              # Wave 3 - Independence Ph1（後段で更新）
            "acc_bonus": scores.get("acc_bonus", 0.0),   # ★ I-1追加
            "earnings_acceleration": scores.get("earnings_acceleration"),  # ★ I-1追加
            # [I-2] 保守度スコアと複合フラグ
            "conservatism_score": (
                surprise_cache.get(ticker, {}).get("conservatism_score", 50.0)
                if surprise_cache else 50.0
            ),
            "is_high_prob_surprise": (
                scores.get("acc_bonus", 0) >= 7.0
                and (surprise_cache.get(ticker, {}).get("conservatism_score", 0) or 0) >= 70.0
            ) if surprise_cache else False,
            "is_negative_risk": (
                scores.get("acc_bonus", 0) <= -7.0
                and (surprise_cache.get(ticker, {}).get("conservatism_score", 0) or 0) <= 30.0
            ) if surprise_cache else False,
            "bq_bonus":             scores.get("bq_bonus", 0.0),   # ★ P-2
            # [I-3] 経営者スコア
            "ceo_score":            ceo_score_cached,
            "ceo_bonus":            ceo_bonus,
            "ml_correction":        0.0,   # ★ RESEARCH-4追加（main.py で後から書き込む）
            # [GAP-4] 戦略ラベル（後段で付与・ここでは空文字）
            "strategy_label":     "",
            "hold_weeks":       0,
            "entry_trigger":    "",
            "exit_trigger":     "",
            "conviction":       "",
            "signal_count":     0,
            "gross_margin":         data.get("gross_margin"),
            "op_margin":            data.get("op_margin"),
            "cf_quality_ratio":     bq_detail.get("cf_ratio"),     # CF/純利益比率
            # [M-3] アクティビスト候補
            "activist_score":         activist_score,
            "activist_flags":         activist_flags,
            "is_activist_target":     activist_score >= 70.0,
            # [M-1] ネットキャッシュリッチフラグ
            "is_net_cash_rich":       (
                (data.get("total_cash", 0) or 0)
                - (data.get("total_debt") or 0)
            ) / ((data.get("market_cap") or 1) or 1) >= getattr(
                cfg, "NET_CASH_RICH_THRESHOLD", 0.30
            ) if data.get("market_cap") else False,
            # [P-3] 長期保有候補
            "is_longterm_hold":       is_longterm,
            "longterm_confidence":    lt_confidence,
            "longterm_flags":         lt_flags,
            "net_cash_ratio":         (
                round((data.get("total_cash", 0) - (data.get("total_debt") or 0))
                      / (data.get("market_cap", 1) or 1), 3)
                if data.get("market_cap") else None
            ),
        }
        candidates.append(candidate)

        # 全銘柄スコア参照リストに追加
        if all_scores_ref is not None:
            all_scores_ref.append(scores["composite_score"])

    # ── [Wave 3 - Independence Ph1] Independence Score バッチ計算 ──
    try:
        # Wave 7: independence_scores.json のキャッシュを優先参照し、
        # キャッシュ未存在の銘柄のみ calc_independence_score_for_ticker() で計算
        from independence_score_calculator import (
            calc_independence_score_for_ticker as _calc_ind,
            get_independence_score             as _get_ind_cached,
            get_catalyst_flags                 as _get_cat_flags,
            get_combined_score                 as _get_comb,
        )
        for _c in candidates:
            if not _c.get("pass_filters"):
                continue
            _t    = _c.get("ticker", "")
            _code = str(_t).split(".")[0]

            # ① キャッシュ優先
            _cached_ind = _get_ind_cached(_t)
            if _cached_ind is not None:
                _c["independence_score"] = float(_cached_ind)
                _c["combined_score"]     = float(_get_comb(_t) or 0.0)
                _c["catalyst_flags"]     = _get_cat_flags(_t) or []
            else:
                # ② キャッシュなし → 個別計算（上限20銘柄）
                _ind_result = _calc_ind(_t, market="JP")
                _c["independence_score"] = float(
                    _ind_result.get("independence_score", 0.0)
                )
                _c["combined_score"]  = float(
                    _ind_result.get("combined_score", 0.0)
                )
                _c["catalyst_flags"]  = _ind_result.get("catalyst_flags", [])

            # ③ latent_pearl_cache から潜伏Pearlスコアを補完
            _lp_score = 0.0
            try:
                # Wave 7: run_latent_pearl_manager をキャッシュ優先で呼び出す
                # （force_refresh=False でキャッシュ TTL 内なら再計算しない）
                from latent_pearl_manager import (
                    run_latent_pearl_manager as _run_lpm_w7,
                    get_promoted_tickers     as _get_promoted,
                )
                # キャッシュが存在しない場合のみ実行
                _promoted = _get_promoted()
                if not _promoted:
                    _run_lpm_w7(cfg=cfg, force_refresh=False)
            except Exception:
                    pass
            _c["latent_pearl_score"] = _lp_score

            # ④ is_latent_pearl 判定（Wave 7版: latent_pearl_cache 優先）
            _c["is_latent_pearl"] = _lp_score >= 30.0

            # ⑤ composite_score への加算
            # latent_pearl_score が高いほど大きいボーナス（最大 +5pt）
            if _c["is_latent_pearl"] and _c.get("pass_filters"):
                _lp_bonus = round(min(5.0, _lp_score / 20.0), 1)
                _c["composite_score"] = round(
                    min(100.0, _c.get("composite_score", 0.0) + _lp_bonus), 1
                )
                _c["latent_pearl_bonus"] = _lp_bonus
            else:
                _c["latent_pearl_bonus"] = 0.0

    except Exception as _inde:
        pass   # Independence Score 失敗でもスクリーニングを継続

    # ── [Wave 3 - Independence Ph1] Latent Pearl Manager 更新 ────
    try:
        from independence_score_calculator import get_latent_pearl_candidates as _glc
        from latent_pearl_manager import run_latent_pearl_manager as _run_lpm
        _latent_cands = _glc(
            {c.get("ticker", ""): {
                "independence_score": c.get("independence_score", 0.0),
                "phase": "LATENT_PEARL" if c.get("is_latent_pearl") else "UNKNOWN",
                "beta_sector": 1.0,
                "epsilon_mean": 0.0,
                "sector_code": sector_code,
             }
             for c in candidates if c.get("independence_score", 0) >= 70
            }
        )
        if _latent_cands:
            _run_lpm(_latent_cands, cfg=cfg)
    except Exception:
        pass

    # ログ出力
    passed_list = [c for c in candidates if c["pass_filters"]]
    print(f"      → 通過: {len(passed_list)}/{len(candidates)}銘柄"
          + (f" | EDINET accrual: {sum(1 for t,_ in raw_data_list if t in accruals)}銘柄"
             if accruals else ""))

    # ── [Wave 3 - Independence Ph1] Promoted Ticker を先頭挿入 ──
    try:
        # Wave 7: get_latent_pearls_list() から昇格対象を取得して先頭に挿入
        from latent_pearl_manager import (
            get_latent_pearls_list as _get_lpl,
            get_promoted_tickers   as _get_prom,
        )
        _promoted_tickers = set(_get_prom())
        _latent_top = [
            e for e in _get_lpl(min_score=70.0)
            if e.get("ticker") in _promoted_tickers
        ]
        # 既存 candidates に含まれていない昇格銘柄を先頭に挿入
        _existing_tickers = {c.get("ticker", "") for c in candidates}
        for _lpe in _latent_top:
            _lp_ticker = _lpe.get("ticker", "")
            if _lp_ticker and _lp_ticker not in _existing_tickers:
                candidates.insert(0, {
                    "ticker":             _lp_ticker,
                    "pass_filters":       True,
                    "promoted_by_latent": True,
                    "latent_pearl_score": _lpe.get("latent_pearl_score", 0.0),
                    "independence_score": _lpe.get("independence_score", 0.0),
                    "catalyst_flags":     _lpe.get("catalyst_flags", []),
                    "composite_score":    round(
                        min(100.0, 50.0 + _lpe.get("latent_pearl_score", 0.0) * 0.3), 1
                    ),
                    "sector_code":        sector_code,
                    "size_tier":          "Micro",  # 潜伏銘柄はデフォルトMicro
                })
                _existing_tickers.add(_lp_ticker)
    except Exception:
        pass   # 挿入失敗でも既存 candidates をそのまま返す

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
        # [M-3] 触媒待ち割安候補
        activist_top_local = sorted(
            [c for c in result["candidates"] if c.get("is_activist_target")],
            key=lambda x: x.get("activist_score", 0), reverse=True,
        )[:10]
        if activist_top_local:
            lines += [
                "",
                "## 🎯 触媒待ち割安候補（activist_score ≥ 70）",
                "",
                "| Ticker | 銘柄名 | Aスコア | 主な根拠 |",
                "|--------|--------|--------|---------|",
            ]
            for c in activist_top_local:
                flags = " / ".join(c.get("activist_flags", [])[:3])
                lines.append(
                    f"| {c['ticker']} | {c['name'][:18]} "
                    f"| {c.get('activist_score', 0):.0f} | {flags} |"
                )
        out = screen_dir / f"screener_{today}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  💾 スクリーニングレポート保存: screener_{today}.md")

    except Exception as e:
        print(f"  ⚠️  スクリーニングレポート保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 5: メインエントリポイント（v3.0: S-2/S-3統合）
# ==============================================================

def run_stock_screener(cfg, sector_results: dict,
                       sector_tier_map: dict | None = None) -> dict:
    """
    sector_tier_map : results["sector_tier"] の dict
                      {sector_code: "TOP"/"MID"/"BOT"}
                      省略時は全業種を "TOP" として扱う（後方互換）
    """
    sector_tier_map = sector_tier_map or {}
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
    # [問題D] 全銘柄リストを sector_results から構築
    from sector_tickers import JP_SECTOR_TICKERS as _jst_d
    all_tickers: list = [t for tlist in _jst_d.values() for t in tlist]
    dynamic_ticker_meta: dict = {}
    # ── [問題D] 動的追加銘柄をユニバースに追加 ────────────────
    dynamic_tickers = getattr(cfg, "DYNAMIC_SCREENER_TICKERS", [])
    dynamic_meta    = getattr(cfg, "DYNAMIC_SCREENER_META", {})

    if dynamic_tickers:
        # 静的リストに未含有の銘柄を追加
        base_set = set(all_tickers)   # all_tickers は既存の全銘柄リスト # type: ignore
        for t in dynamic_tickers:
            if t not in base_set:
                all_tickers.append(t)  # type: ignore
                # discovery_score フラグを立てる（後でcandidateに反映）
                if t in dynamic_meta:
                    dynamic_ticker_meta[t] = dynamic_meta[t]  # type: ignore
        print(f"    📋 [問題D] 動的銘柄を追加: "
              f"{len(dynamic_tickers) - len(base_set & set(dynamic_tickers))}社")
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
            sector_tier     = sector_tier_map.get(code, "TOP"),  # ★ GAP-6追加
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
        # ── [問題D] 動的追加銘柄は discovery_score を付与 ────────
        _dyn_meta = getattr(cfg, "DYNAMIC_SCREENER_META", {})
        for candidate in candidates:
            _t = candidate.get("ticker", "")
            if _t in _dyn_meta:
                candidate["discovery_score"] = _dyn_meta[_t].get("discovery_score", 100)
                candidate["dynamic_source"]  = _dyn_meta[_t].get("source", "")
        all_candidates.extend(candidates)
    # ── [問題D] 動的追加銘柄のスクリーニング ─────────────────────
    _dyn_tickers = getattr(cfg, "DYNAMIC_SCREENER_TICKERS", [])
    _dyn_meta    = getattr(cfg, "DYNAMIC_SCREENER_META", {})
    if _dyn_tickers:
        # 既に静的ループで処理済みの ticker を除外
        _screened = {c.get("ticker", "") for c in all_candidates}
        _dyn_new  = [t for t in _dyn_tickers if t not in _screened]
        if _dyn_new:
            print(f"    📋 [問題D] 動的追加銘柄: {len(_dyn_new)}社をスクリーニング...")
            # セクターコード不明のため、「未分類」として処理する
            # セクタースコアは MID相当（50.0）でフォールバック
            _dyn_candidates = _screen_sector(
                sector_code     = "DYNAMIC",
                sector_name     = "動的追加",
                sector_score    = 50.0,
                tickers         = _dyn_new,
                cfg             = cfg,
                market          = "JP",
                edinet_accruals = edinet_accruals,
                all_scores_ref  = all_comp_scores,
                surprise_cache  = surprise_cache,
                credit_cache    = credit_cache,
                tech_cache      = tech_cache,
                insider_cache   = insider_cache,
                dividend_cache  = dividend_cache,
                sr_cache        = sr_cache,
                vp_cache        = vp_cache,
                edinet_quality_cache = edinet_quality_cache,
                comps_cache     = comps_cache,
                dcf_cache       = dcf_cache,
            )
            # 動的追加銘柄に discovery_score フラグを付与
            for c in _dyn_candidates:
                t = c.get("ticker", "")
                if t in _dyn_meta:
                    c["discovery_score"] = _dyn_meta[t].get("discovery_score", 100)
                    c["dynamic_source"]  = _dyn_meta[t].get("source", "")
                    c["edinet_freshness"] = None  # ★ ④追加（鮮度スコア / None=未計算）
                    c["xbrl_freshness"] = None # ★ ④追加（US財務鮮度）
            all_candidates.extend(_dyn_candidates)
            print(f"    ✅ 動的銘柄 {len(_dyn_candidates)}社 追加完了")
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
        # [GAP-4] 戦略ラベル付与
        c["strategy_label"]  = _assign_strategy_label(c, cfg)
        if disc_score >= 40:
            discovery_count += 1

    # 結果集計
    passed = [c for c in all_candidates if c["pass_filters"]]
    discovery_top = sorted(
        [c for c in all_candidates if c.get("discovery_score", 0) >= 40],
        key=lambda x: x.get("discovery_score", 0),
        reverse=True,
    )[:10]
    # [M-3] アクティビスト候補トップ10
    activist_top = sorted(
        [c for c in all_candidates
         if c.get("is_activist_target") and c.get("pass_filters")],
        key=lambda x: x.get("activist_score", 0),
        reverse=True,
    )[:10]
    # [M-1] ネットキャッシュリッチ候補
    net_cash_rich_top = sorted(
        [c for c in all_candidates
         if c.get("is_net_cash_rich") and c.get("pass_filters")],
        key=lambda x: x.get("net_cash_ratio", 0) or 0,
        reverse=True,
    )[:10]
    # [P-3] 長期保有候補
    longterm_top = sorted(
        [c for c in all_candidates
         if c.get("is_longterm_hold") and c.get("pass_filters")],
        key=lambda x: x.get("composite_score", 0),
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

    # [GAP-4] 戦略別リスト
    _passed = [c for c in all_candidates if c.get("pass_filters")]
    core_top = sorted(
        [c for c in _passed if c.get("strategy_label") == "core"],
        key=lambda x: x.get("composite_score", 0), reverse=True,
    )[:15]
    satellite_event_top = sorted(
        [c for c in _passed if c.get("strategy_label") == "satellite_event"],
        key=lambda x: x.get("composite_score", 0), reverse=True,
    )[:10]
    satellite_smallcap_top = sorted(
        [c for c in _passed if c.get("strategy_label") == "satellite_smallcap"],
        key=lambda x: x.get("activist_score", 0) or x.get("composite_score", 0), reverse=True,
    )[:10]
    satellite_info_top = sorted(
        [c for c in _passed if c.get("strategy_label") == "satellite_info"],
        key=lambda x: x.get("discovery_score", 0), reverse=True,
    )[:10]

    result = {
        "candidates":              all_candidates,
        "screened_count":          len(all_candidates),
        "pass_count":              len(passed),
        "top_sectors":             top_sector_names,
        "discovery_top":           discovery_top,
        "activist_top":            activist_top,
        "net_cash_rich_top":       net_cash_rich_top,
        "longterm_top":            longterm_top,
        # [GAP-4] 戦略別
        "core_top":                core_top,
        "satellite_event_top":     satellite_event_top,
        "satellite_smallcap_top":  satellite_smallcap_top,
        "satellite_info_top":      satellite_info_top,
        "timestamp":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    _save_screener_report(result, cfg)

    # ── [R-3] investment_journal への自動エントリ生成 ──────────
    try:
        import sys as _sys, importlib as _imp
        _sys.path.insert(0, str(Path(__file__).parent))
        _ij = _imp.import_module("investment_journal")
        _ij.auto_create_entries(cfg, result)
    except Exception as _e:
        pass  # journal失敗はscreenerを止めない

    return result


# ==============================================================
# 単体テスト
# ==============================================================
if __name__ == "__main__":
    import importlib.util
    from pathlib import Path

    cfg = None
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            # 波線対策：spec と loader が存在することを確定させる
            if spec is not None and spec.loader is not None:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    mock_sectors = {
        "1016": {"composite_score": 78.0, "sector_name_ja": "電気機器"},
        "1017": {"composite_score": 65.0, "sector_name_ja": "輸送用機器"},
    }

    # cfg が存在するときだけ実行すれば、これ以降の cfg に関する波線はすべて消えます
    if cfg:
        result = run_stock_screener(cfg, mock_sectors)
        print(f"\n通過銘柄数: {result['pass_count']}")
        print(f"未発見候補: {len(result['discovery_top'])}")
        
        for c in sorted(result["candidates"],
                        key=lambda x: x["composite_score"], reverse=True)[:5]:
            if c["pass_filters"]:
                print(f"  {c['ticker']} | PER:{c['per']} | ROE:{c['roe']}% "
                      f"| score:{c['composite_score']} | accrual:{c['accrual_tier']} "
                      f"| disc:{c['discovery_score']:.0f}")
