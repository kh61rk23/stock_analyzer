# ==============================================================
# business_model_normalizer.py  v1.0  (BM Ph2)
# 業態別スコア正規化・bm_bonus算出
# ==============================================================
# 【役割】
#   銘柄の業態（BM Normalization Type）を判定し、
#   通常スコアリングでは過小/過大評価される業態固有メトリクスを
#   正規化・補正して bm_bonus を返す。
#
# 【BM正規化タイプ（本ファイル独自の分類）】
#   NORM_SHOSHA   : 商社型  → 利益/純資産（ROE超過分）でボーナス
#   NORM_BANK     : 銀行型  → ROA×レバレッジ調整でボーナス
#   NORM_INSURANCE: 保険型  → ROE + 配当利回りでボーナス
#   NORM_STANDARD : 標準型  → 正規化なし（bonus = 0.0）
#
#   ※ config.py BLOCK 25 の BM-01〜BM-12 とは別の分類体系。
#     本ファイルは「スコア正規化専用」の簡易分類を使用する。
#
# 【呼び出しパターン】
#   # base_agent.py から（data = fetch_stock_data() の 1銘柄辞書）
#   from business_model_normalizer import calc_bm_bonus, detect_bm_norm_type
#   bm_type = detect_bm_norm_type(sector_code="1026", market="JP", cfg=cfg)
#   bonus   = calc_bm_bonus(ticker, data, sector_code="1026", market="JP", cfg=cfg)
#
#   # stock_screener.py から（_calc_scores() 内）
#   from business_model_normalizer import normalize_value_score_by_bm
#   value_score = normalize_value_score_by_bm(value_score, bm_norm_type, data)
#
# 【設計原則】
#   - bm_bonus の値域: $-5.0 \leq bonus \leq 10.0$（quality_score への加算用）
#   - 全関数を try/except で保護。失敗時は bonus=0.0 を返す。
#   - ROE/ROA は yfinance 生値（小数表記: 0.15 = 15%）を前提とする。
#
# 【依存】
#   config.py (BLOCK 25: BM_ROE_BENCHMARKS, BM_SECTOR_MAP 参照)
#   subtheme_loader.py (get_bm_type() 参照)
#   追加ライブラリなし（標準ライブラリのみ）
# ==============================================================

from __future__ import annotations

from typing import Optional

# ==============================================================
# BLOCK 0: BM 正規化タイプ定数
# ==============================================================

NORM_SHOSHA    = "NORM_SHOSHA"     # 商社型
NORM_BANK      = "NORM_BANK"       # 銀行型
NORM_INSURANCE = "NORM_INSURANCE"  # 保険型
NORM_STANDARD  = "NORM_STANDARD"   # 標準型（正規化なし）
NORM_SAAS      = "NORM_SAAS"       # SaaS型（ARR/NRR基準・BM Ph3）

# JP セクターコード → BM 正規化タイプ の直接マッピング
_SECTOR_TO_NORM_JP: dict[str, str] = {
    "1026": NORM_SHOSHA,      # 卸売業（総合商社）
    "1028": NORM_BANK,        # 銀行業
    "1029": NORM_STANDARD,    # 証券・商品先物（個別スコアが有効）
    "1030": NORM_INSURANCE,   # 保険業
    "1031": NORM_STANDARD,    # その他金融
}

# US セクターコード → BM 正規化タイプ の直接マッピング
_SECTOR_TO_NORM_US: dict[str, str] = {
    "US_40": NORM_BANK,    # Financials（銀行・金融全般）
    "US_45": NORM_SAAS,   # Information Technology（SaaS企業が多い）
    "US_50": NORM_SAAS,   # Communication Services（SaaS/プラットフォーム）
}

# subtheme_loader の bm_type 文字列 → BM 正規化タイプ マッピング
_SUBTHEME_BM_TO_NORM: dict[str, str] = {
    "BM-01":       NORM_SHOSHA,      # subtheme JSON 内の商社タグ
    "BM-02":       NORM_BANK,        # subtheme JSON 内の銀行タグ
    "BM-03":       NORM_INSURANCE,   # subtheme JSON 内の保険タグ
    "BM-US-FIN":   NORM_BANK,        # US 金融
    "BM-US-UTIL":  NORM_STANDARD,    # US 公益（公益は個別スコア有効）
    "BM-US-SAAS":  NORM_SAAS,        # US SaaS / プラットフォーム
}

# config.py BM_SECTOR_MAP の BM タイプ → BM 正規化タイプ マッピング
_CONFIG_BM_TO_NORM: dict[str, str] = {
    "BM-06": NORM_BANK,      # 金融サービス（銀行・保険・証券）
    "BM-09": NORM_SHOSHA,    # 小売・流通・卸売 → 商社は BM-09 にマップされている
}

# bm_bonus 上下限（quality_score への加算値）

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'BMN_BONUS_MAX': '_BONUS_MAX',
    'BMN_BONUS_MIN': '_BONUS_MIN',
}

_BONUS_MAX =  10.0
_BONUS_MIN =  -5.0

# ==============================================================
# BLOCK 1: BM 正規化タイプ判定
# ==============================================================

def detect_bm_norm_type(
    sector_code:   Optional[str] = None,
    market:        str           = "JP",
    cfg=None,
    subtheme_info: Optional[dict] = None,
) -> str:
    """
    銘柄の BM 正規化タイプを判定する。

    判定優先順:
      1. sector_code の直接マッピング（最高精度）
      2. subtheme_info["bm_type"] 経由のマッピング
      3. config.py の BM_SECTOR_MAP 経由のマッピング
      4. NORM_STANDARD にフォールバック

    Args:
        sector_code:   "1026" / "US_40" 形式
        market:        "JP" or "US"
        cfg:           config モジュール（None 可）
        subtheme_info: subtheme_loader.load_subtheme() の返り値（None 可）

    Returns:
        NORM_SHOSHA | NORM_BANK | NORM_INSURANCE | NORM_STANDARD
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    try:
        sc = str(sector_code).strip() if sector_code else ""

        # ── ① sector_code 直接マッピング ─────────────────────
        lookup = _SECTOR_TO_NORM_JP if market == "JP" else _SECTOR_TO_NORM_US
        if sc in lookup:
            return lookup[sc]

        # ── ② subtheme_info["bm_type"] 経由 ──────────────────
        if subtheme_info:
            bm_tag = subtheme_info.get("bm_type")
            if bm_tag and bm_tag in _SUBTHEME_BM_TO_NORM:
                return _SUBTHEME_BM_TO_NORM[bm_tag]

        # ── ③ config.py BM_SECTOR_MAP 経由 ───────────────────
        if cfg is not None:
            bm_map: dict = getattr(cfg, "BM_SECTOR_MAP", {})
            bm_types: list = bm_map.get(sc, [])
            for bm in bm_types:
                if bm in _CONFIG_BM_TO_NORM:
                    return _CONFIG_BM_TO_NORM[bm]

        # ── ④ NORM_STANDARD にフォールバック ─────────────────
        return NORM_STANDARD

    except Exception:
        return NORM_STANDARD


def get_roe_benchmark(bm_norm_type: str, cfg=None) -> float:
    """
    BM 正規化タイプに応じた ROE ベンチマーク（小数表記）を返す。

    Returns:
        float: ROE benchmark (例: 0.12 = 12%)
    """
    try:
        # config.py の BM_ROE_BENCHMARKS をベースに、業態別に上書き
        defaults: dict[str, float] = {
            NORM_SHOSHA:    0.12,   # 商社目標 ROE 12%以上
            NORM_BANK:      0.06,   # 銀行 ROE は低め（自己資本比率制約）
            NORM_INSURANCE: 0.07,   # 保険 ROE
            NORM_STANDARD:  0.08,   # 汎用ベンチマーク
        }
        if cfg is not None:
            # config.py の BM_ROE_BENCHMARKS["BM-09"] = 0.08（卸売）等を参照
            bm_benchmarks: dict = getattr(cfg, "BM_ROE_BENCHMARKS", {})
            if bm_norm_type == NORM_SHOSHA:
                # BM-09（卸売）のベンチマークを取得し、商社補正で+0.04
                base = float(bm_benchmarks.get("BM-09", 0.08))
                defaults[NORM_SHOSHA] = max(0.10, base + 0.04)
            elif bm_norm_type == NORM_BANK:
                defaults[NORM_BANK] = float(bm_benchmarks.get("BM-06", 0.06))

        return defaults.get(bm_norm_type, 0.08)

    except Exception:
        return 0.08


# ==============================================================
# BLOCK 2: 各業態の bonus 計算
# ==============================================================

def _safe_float(val, default: float = 0.0) -> float:
    """None / 非数値を安全に float に変換する。"""
    try:
        if val is None:
            return default
        v = float(val)
        return v if (v == v and abs(v) < 1e15) else default  # NaN/Inf除外
    except Exception:
        return default


def _clamp(val: float, lo: float = _BONUS_MIN, hi: float = _BONUS_MAX) -> float:
    """値を [lo, hi] にクランプする。"""
    return max(lo, min(hi, val))


def _calc_shosha_bonus(
    data: dict,
    cfg=None,
) -> float:
    """
    商社型 (NORM_SHOSHA) のボーナスを算出する。

    【計算式】
    $bonus_{ROE} = (ROE - benchmark) \times 50$
    $bonus_{PBR} = \\max(0, 1.5 - PBR) \times 2$ （PBR が低すぎる場合の補正）
    $bonus = \text{clamp}(bonus_{ROE} + bonus_{PBR}, -5.0, 10.0)$

    設計根拠:
    - 商社の ROE は持分法投資利益を含む多角的な利益源泉に基づく。
    - benchmark 12% を超過した 1% につき 0.5点加算（最大 +7点）。
    - PBR 1.5 以下は市場の低評価として補正（最大 +2点）。
    - 合計上限 10点、下限 -5点。

    Args:
        data: base_agent.py の fetch_stock_data() 返り値辞書
        cfg:  config モジュール（None 可）

    Returns:
        float: bm_bonus (-5.0 〜 10.0)
    """
    try:
        roe = _safe_float(data.get("roe"))
        pbr = _safe_float(data.get("pbr"))

        benchmark = get_roe_benchmark(NORM_SHOSHA, cfg)

        # ROE 超過ボーナス: (ROE - benchmark) × 50
        # 例: ROE=18%, benchmark=12% → (0.18-0.12)×50 = 3.0点
        bonus_roe = (roe - benchmark) * 50.0 if roe > 0 else 0.0

        # PBR 低評価補正: PBR が 1.5 以下は「解散価値割れに近い」として加点
        # 例: PBR=0.8 → (1.5-0.8)×2 = 1.4点
        bonus_pbr = 0.0
        if 0.0 < pbr < 1.5:
            bonus_pbr = (1.5 - pbr) * 2.0

        # EV/EBITDA ペナルティ: 高すぎる場合は商社資産の水増しリスク
        # ev_ebitda > 20 → -1点のペナルティ
        ev_ebitda = _safe_float(data.get("ev_ebitda"))
        bonus_ev = -1.0 if (ev_ebitda > 20.0 and ev_ebitda > 0) else 0.0

        total = bonus_roe + bonus_pbr + bonus_ev
        return _clamp(total)

    except Exception:
        return 0.0


def _calc_bank_bonus(
    data: dict,
    cfg=None,
) -> float:
    """
    銀行型 (NORM_BANK) のボーナスを算出する。

    【計算式】
    $bonus_{ROA} = (ROA - benchmark_{ROA}) \times 300$
    $bonus_{PBR} = (1.0 - PBR) \times 3$ （PBR < 1 は銀行では割安シグナル）
    $penalty_{DTE} = -2.0$ （debtToEquity > 800 で負債過剰ペナルティ）
    $bonus = \text{clamp}(bonus_{ROA} + bonus_{PBR} + penalty_{DTE}, -5.0, 8.0)$

    設計根拠:
    - 銀行の ROE は自己資本比率規制で構造的に抑制される。
    - ROA で収益性を評価（JP 銀行の平均 ROA ≈ 0.3〜0.5%）。
    - PBR 1.0 未満は金利上昇恩恵の先行指標として補正。
    - debtToEquity は銀行では通常 500〜1500 と高く、
      800 超はペナルティなし（銀行業の構造的特性）。
      ただし 1500 超は過剰負債としてペナルティ。

    Returns:
        float: bm_bonus (-5.0 〜 8.0)
    """
    try:
        roa = _safe_float(data.get("roa"))
        pbr = _safe_float(data.get("pbr"))
        dte = _safe_float(data.get("debt_to_equity"))

        # JP 銀行の ROA ベンチマーク ≈ 0.3%
        roa_benchmark = 0.003

        # ROA ベースボーナス
        # 例: ROA=0.5% → (0.005-0.003)×300 = 0.6点
        bonus_roa = (roa - roa_benchmark) * 300.0 if roa > 0 else 0.0

        # PBR 割安補正（銀行において PBR < 1 は金利上昇メリットの先行指標）
        # PBR = 0.5 → (1.0-0.5)×3 = 1.5点
        bonus_pbr = 0.0
        if 0.0 < pbr < 1.5:
            bonus_pbr = (1.0 - min(pbr, 1.0)) * 3.0

        # 負債過剰ペナルティ（銀行でも DTE > 1500 は過剰）
        penalty_dte = -2.0 if dte > 1500.0 and dte > 0 else 0.0

        # net_margin が極端に低い場合は収益悪化ペナルティ
        net_m = _safe_float(data.get("net_margin"))
        penalty_nm = -1.5 if (net_m < 0.0 and net_m != 0.0) else 0.0

        total = bonus_roa + bonus_pbr + penalty_dte + penalty_nm
        return _clamp(total, _BONUS_MIN, 8.0)

    except Exception:
        return 0.0


def _calc_insurance_bonus(
    data: dict,
    cfg=None,
) -> float:
    """
    保険型 (NORM_INSURANCE) のボーナスを算出する。

    【計算式】
    $bonus_{ROE} = (ROE - benchmark_{INS}) \times 40$
    $bonus_{div} = \\min(dividend\\_yield \times 80, 3.0)$
    $bonus = \text{clamp}(bonus_{ROE} + bonus_{div}, -5.0, 8.0)$

    設計根拠:
    - 保険会社の利益は「保険引受利益」＋「資産運用収益」の合計。
    - 配当利回りは安定収益の代理変数として加算（上限 3点）。
    - ROE ベンチマーク 7%（保険業界平均）超過分でボーナス。

    Returns:
        float: bm_bonus (-5.0 〜 8.0)
    """
    try:
        roe = _safe_float(data.get("roe"))
        div = _safe_float(data.get("dividend_yield"))

        benchmark = get_roe_benchmark(NORM_INSURANCE, cfg)

        # ROE 超過ボーナス
        bonus_roe = (roe - benchmark) * 40.0 if roe > 0 else 0.0

        # 配当利回りボーナス（配当は運用収益の安定性代理変数）
        # div=4% → 0.04×80 = 3.2 → min(3.2, 3.0) = 3.0点
        bonus_div = min(div * 80.0, 3.0) if div > 0 else 0.0

        # 過大な payout_ratio はペナルティ
        payout = _safe_float(data.get("payout_ratio"))
        penalty_po = -1.0 if payout > 1.0 and payout > 0 else 0.0

        total = bonus_roe + bonus_div + penalty_po
        return _clamp(total, _BONUS_MIN, 8.0)

    except Exception:
        return 0.0

def _calc_saas_bonus(
    data: dict,
    cfg=None,
) -> float:
    """
    SaaS型 (NORM_SAAS) のボーナスを算出する（BM Ph3）。

    【SaaS固有指標の proxy 計算】
    yfinance から直接 ARR / NRR を取得できないため、
    利用可能なフィールドで近似する:
      ARR proxy    : totalRevenue（年次収益 ≒ ARR）
      NRR proxy    : revenue_growth（高成長 ≒ NRR > 100%）
      Gross Margin : grossMargins（SaaS は通常70%以上）
      CAC効率 proxy: PSR（低いほど割安）

    【計算式】
    $bonus_{GM} = \\min(4.0, (gross\\_margin - 0.70) \times 20)$ (GM≥70%で加算)
    $bonus_{growth} = \\min(4.0, revenue\\_growth \times 10)$ (成長率ボーナス)
    $penalty_{ps} = -1.0$ if $PSR > 20$ かつ $revenue\\_growth < 0.15$ (割高ペナルティ)
    $bonus = \text{clamp}(bonus_{GM} + bonus_{growth} + penalty_{ps}, -5.0, 8.0)$

    Returns:
        float: bm_bonus (-5.0〜8.0)
    """
    try:
        gross_margin    = _safe_float(data.get("gross_margin"))
        revenue_growth  = _safe_float(data.get("revenue_growth"))
        psr             = _safe_float(data.get("psr"))
        op_margin       = _safe_float(data.get("op_margin"))

        # ── Gross Margin ボーナス（SaaS基準 70%以上）────────────
        # GM=75% → (0.75-0.70)×20 = 1.0pt
        # GM=90% → (0.90-0.70)×20 = 4.0pt（上限）
        bonus_gm = 0.0
        if gross_margin > 0:
            if gross_margin >= 0.70:
                bonus_gm = min(4.0, (gross_margin - 0.70) * 20.0)
            elif gross_margin >= 0.50:
                bonus_gm = 0.0   # ニュートラル（SaaS水準未満だが許容範囲）
            else:
                bonus_gm = -2.0  # 低GM → ペナルティ

        # ── NRR proxy（revenue_growth）ボーナス ────────────────
        # growth=20% → 0.20×10 = 2.0pt
        # growth=40% → 0.40×10 = 4.0pt（上限）
        bonus_growth = 0.0
        if revenue_growth > 0:
            bonus_growth = min(4.0, revenue_growth * 10.0)
        elif revenue_growth < -0.10:
            bonus_growth = -2.0  # 売上減少 → ペナルティ

        # ── PSR 過大評価ペナルティ ──────────────────────────────
        # PSR > 20 かつ revenue_growth < 15% → 高バリュエーション過ぎ
        penalty_ps = 0.0
        if psr > 20.0 and psr > 0 and revenue_growth < 0.15:
            penalty_ps = -1.0

        # ── 赤字でも成長なら許容（SaaS成長フェーズ） ────────────
        # operating_margin が大きく赤字の場合のみ追加ペナルティ
        penalty_op = 0.0
        if op_margin < -0.30 and op_margin != 0.0:
            penalty_op = -1.0  # 営業赤字率30%超 → 軽微なペナルティ

        total = bonus_gm + bonus_growth + penalty_ps + penalty_op
        return _clamp(total, _BONUS_MIN, 8.0)

    except Exception:
        return 0.0

# ==============================================================
# BLOCK 3: 公開 API
# ==============================================================

def calc_bm_bonus(
    ticker:      str,
    data:        dict,
    sector_code: Optional[str] = None,
    market:      str           = "JP",
    cfg=None,
    subtheme_info: Optional[dict] = None,
) -> float:
    """
    業態別 bm_bonus を算出するメインエントリポイント。

    base_agent.py から呼ぶ:
        from business_model_normalizer import calc_bm_bonus
        try:
            bm_bonus = calc_bm_bonus(
                ticker, data, sector_code=sector_code,
                market=market, cfg=cfg
            )
        except Exception:
            bm_bonus = 0.0

    Args:
        ticker:        銘柄コード（ログ用）
        data:          base_agent.py の fetch_stock_data() 返り値辞書
        sector_code:   "1026" / "US_40" 形式（None 可）
        market:        "JP" or "US"
        cfg:           config モジュール（None 可）
        subtheme_info: subtheme_loader.load_subtheme() の返り値（None 可）

    Returns:
        float: bm_bonus (-5.0 〜 10.0)
        ※ NORM_STANDARD の場合は 0.0
    """
    try:
        bm_type = detect_bm_norm_type(
            sector_code=sector_code,
            market=market,
            cfg=cfg,
            subtheme_info=subtheme_info,
        )

        if bm_type == NORM_SHOSHA:
            bonus = _calc_shosha_bonus(data, cfg)
        elif bm_type == NORM_BANK:
            bonus = _calc_bank_bonus(data, cfg)
        elif bm_type == NORM_INSURANCE:
            bonus = _calc_insurance_bonus(data, cfg)
        elif bm_type == NORM_SAAS:
            bonus = _calc_saas_bonus(data, cfg)    # BM Ph3
        else:
            bonus = 0.0

        return round(bonus, 2)

    except Exception:
        return 0.0


def normalize_value_score_by_bm(
    value_score:  float,
    bm_norm_type: str,
    data:         dict,
    cfg=None,
) -> float:
    """
    業態別に value_score を正規化して返す。
    stock_screener.py の _calc_scores() 内で呼ぶ。

    【業態別正規化ロジック】
    - NORM_BANK    : PBR < 1.0 を「割安」として加点（銀行では正常範囲）。
                     通常スコアは PBR 低値をそのまま高評価するため調整不要だが、
                     PBR > 2.0 の銀行は過大評価として -5点補正。
    - NORM_SHOSHA  : EV/EBITDA が 15 超の場合、value_score を -3点補正。
                     商社の EBITDA には持分法利益が含まれず過小評価になる場合がある。
    - NORM_INSURANCE: PBR 補正なし（ROE ベースで十分）。
    - NORM_STANDARD: 変更なし。

    Args:
        value_score:  現在の value_score (0.0 〜 100.0)
        bm_norm_type: NORM_* 定数
        data:         財務データ辞書
        cfg:          config モジュール（None 可）

    Returns:
        float: 正規化後の value_score (0.0 〜 100.0)
    """
    try:
        adj = 0.0

        if bm_norm_type == NORM_BANK:
            pbr = _safe_float(data.get("pbr"))
            if pbr > 2.0 and pbr > 0:
                adj = -5.0

        elif bm_norm_type == NORM_SHOSHA:
            ev_ebitda = _safe_float(data.get("ev_ebitda"))
            if ev_ebitda > 15.0 and ev_ebitda > 0:
                adj = -3.0

        elif bm_norm_type == NORM_SAAS:
            # SaaS は PER が高くて当然。PSR 基準で判定する。
            # PSR が高くても revenue_growth が高ければ正当化される。
            psr            = _safe_float(data.get("psr"))
            revenue_growth = _safe_float(data.get("revenue_growth"))
            if psr > 25.0 and revenue_growth < 0.10 and psr > 0:
                # 低成長高PSR → 過大評価として割引
                adj = -4.0
            elif psr < 5.0 and revenue_growth > 0.20 and psr > 0:
                # 高成長低PSR → 割安として加点
                adj = 3.0

        normalized = value_score + adj
        return float(max(0.0, min(100.0, normalized)))

    except Exception:
        return float(value_score)


def get_bm_norm_label(bm_norm_type: str) -> str:
    """
    BM 正規化タイプの日本語ラベルを返す（レポート表示用）。

    Returns:
        "商社型" | "銀行型" | "保険型" | "Saas型" | "標準型"
    """
    _labels: dict[str, str] = {
        NORM_SHOSHA:    "商社型",
        NORM_BANK:      "銀行型",
        NORM_INSURANCE: "保険型",
        NORM_SAAS:      "SaaS型",   # BM Ph3
        NORM_STANDARD:  "標準型",
    }
    return _labels.get(bm_norm_type, "標準型")


# ==============================================================
# BLOCK 4: 一括処理ユーティリティ（weekly_report 用）
# ==============================================================

def apply_bm_normalization_batch(
    records:  list[dict],
    market:   str  = "JP",
    cfg=None,
) -> list[dict]:
    """
    スクリーニング結果リストに bm_bonus / bm_norm_type を一括付与する。

    stock_screener.py の _screen_sector() 後処理、または
    weekly_report_generator.py の前処理として使用。

    Args:
        records: [{"ticker": ..., "roe": ..., "pbr": ..., "sector_code": ...}, ...]
        market:  "JP" or "US"
        cfg:     config モジュール（None 可）

    Returns:
        各 dict に "bm_bonus" / "bm_norm_type" / "bm_norm_label" を追加したリスト
    """
    updated = []
    for rec in records:
        try:
            sc   = str(rec.get("sector_code", ""))
            bonus = calc_bm_bonus(
                ticker=rec.get("ticker", ""),
                data=rec,
                sector_code=sc,
                market=market,
                cfg=cfg,
            )
            bm_type = detect_bm_norm_type(sc, market, cfg)
            rec["bm_bonus"]      = bonus
            rec["bm_norm_type"]  = bm_type
            rec["bm_norm_label"] = get_bm_norm_label(bm_type)
        except Exception:
            rec.setdefault("bm_bonus",      0.0)
            rec.setdefault("bm_norm_type",  NORM_STANDARD)
            rec.setdefault("bm_norm_label", "標準型")
        updated.append(rec)
    return updated


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  business_model_normalizer.py v1.0 単体テスト")
    print("=" * 60)

    # ── detect_bm_norm_type テスト ────────────────────────────
    print("\n── BM 正規化タイプ判定 ──")
    test_sectors = [
        ("1026", "JP", "卸売業（商社）"),
        ("1028", "JP", "銀行業"),
        ("1030", "JP", "保険業"),
        ("1016", "JP", "電気機器（標準）"),
        ("US_40", "US", "US Financials"),
        ("US_45", "US", "US IT（標準）"),
    ]
    for sc, mkt, label in test_sectors:
        norm_type = detect_bm_norm_type(sc, mkt)
        print(f"  {label:20s} → {norm_type}  ({get_bm_norm_label(norm_type)})")

    # ── 商社ボーナステスト ────────────────────────────────────
    print("\n── 商社型ボーナス（三菱商事想定値）──")
    shosha_data = {
        "roe": 0.18,         # 18%
        "pbr": 0.95,         # PBR 0.95（低め）
        "ev_ebitda": 8.5,    # 割安圏
    }
    bonus = calc_bm_bonus("8058.T", shosha_data, "1026", "JP")
    print(f"  ROE=18%, PBR=0.95, EV/EBITDA=8.5 → bm_bonus = {bonus:+.2f}点")

    shosha_data2 = {"roe": 0.08, "pbr": 1.6, "ev_ebitda": 22.0}
    bonus2 = calc_bm_bonus("TEST.T", shosha_data2, "1026", "JP")
    print(f"  ROE= 8%, PBR=1.60, EV/EBITDA=22.0 → bm_bonus = {bonus2:+.2f}点")

    # ── 銀行ボーナステスト ────────────────────────────────────
    print("\n── 銀行型ボーナス ──")
    bank_data = {
        "roa": 0.005,          # ROA 0.5%
        "pbr": 0.75,           # PBR 0.75（割安）
        "debt_to_equity": 800.0,
        "net_margin": 0.10,
    }
    bonus_bank = calc_bm_bonus("8306.T", bank_data, "1028", "JP")
    print(f"  ROA=0.5%, PBR=0.75, DTE=800 → bm_bonus = {bonus_bank:+.2f}点")

    bank_data2 = {"roa": 0.002, "pbr": 1.8, "debt_to_equity": 1600.0,
                  "net_margin": -0.01}
    bonus_bank2 = calc_bm_bonus("BANK2.T", bank_data2, "1028", "JP")
    print(f"  ROA=0.2%, PBR=1.80, DTE=1600, 赤字 → bm_bonus = {bonus_bank2:+.2f}点")

    # ── 保険ボーナステスト ─────────────────────────────────────
    print("\n── 保険型ボーナス ──")
    ins_data = {
        "roe": 0.12,           # ROE 12%
        "dividend_yield": 0.035,  # 配当利回り 3.5%
        "payout_ratio": 0.40,
    }
    bonus_ins = calc_bm_bonus("8725.T", ins_data, "1030", "JP")
    print(f"  ROE=12%, 配当3.5%, payout=40% → bm_bonus = {bonus_ins:+.2f}点")

    # ── value_score 正規化テスト ──────────────────────────────
    print("\n── value_score 正規化 ──")
    print(f"  銀行（PBR=2.5）: {normalize_value_score_by_bm(70.0, NORM_BANK, {'pbr': 2.5}):.1f}点")
    print(f"  銀行（PBR=0.8）: {normalize_value_score_by_bm(70.0, NORM_BANK, {'pbr': 0.8}):.1f}点")
    print(f"  商社（EV/EBITDA=18）: "
          f"{normalize_value_score_by_bm(65.0, NORM_SHOSHA, {'ev_ebitda': 18.0}):.1f}点")
    print(f"  標準 : {normalize_value_score_by_bm(65.0, NORM_STANDARD, {}):.1f}点")

    print("\n✅ 単体テスト完了")
