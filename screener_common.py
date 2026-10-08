# ==============================================================
# screener_common.py — JP/US スクリーナー共通ロジック（U-3）
# ==============================================================
# 目的（要件定義書_JP-US統合_20260726.md §6-J2 U-3）:
#   stock_screener.py（JP）と us_stock_screener.py（US）で
#   完全重複 / ほぼ重複の FE関数を1本化する。
#
#   移設対象と対応関係:
#     JP `_calc_risk_adjusted_score`  ⇔ US `_calc_risk_adjusted_score_us`
#         → 完全同一ロジック。そのまま `calc_risk_adjusted_score()` へ。
#     JP `_calc_revision_score`       ⇔ US `_calc_revision_score_us`
#         → 判定表（rec_mean → raw）は同一。倍率・上限のみ市場で異なる。
#            `market` 引数で `config.MARKET_PROFILE` を切り替える。
#     JP `_check_pead_trigger`        ⇔ US `_check_pead_trigger_us`
#         → 完全同一ロジック。そのまま `check_pead_trigger()` へ。
#     JP `_calc_catalyst_bonus`       ⇔ US `_calc_catalyst_bonus_us`
#         → 完全同一ロジック（catalyst_calendar.calc_catalyst_bonus への
#            薄いラッパー）。そのまま `calc_catalyst_bonus()` へ。
#
#   両スクリーナー側の関数は残し、本モジュールへの1行委譲にしている
#   （呼び出し箇所を変えないため）。
#
#   市場別パラメータは config.py BLOCK 61 の `MARKET_PROFILE` を正本とする。
#   既存の *_JP / *_US 設定は文字列の索引として参照し、値を複製しない。
# ==============================================================


def _market_revision_profile(cfg, market: str) -> tuple[str, dict]:
    """実行時cfgのMARKET_PROFILEを読み、未知市場はJPへ安全側フォールバックする。"""
    profiles = getattr(cfg, "MARKET_PROFILE", None)
    if not isinstance(profiles, dict):
        from config import MARKET_PROFILE
        profiles = MARKET_PROFILE
    selected = market if market in profiles else "JP"
    return selected, profiles[selected]


def calc_risk_adjusted_score(
    ticker:          str,
    composite_score: float,
    data:            dict,
    cfg=None,
) -> float:
    """
    FE-1: ATR基準リスク調整スコアを算出する（JP/US共通・U-3で移設）。

    yfinance の hist データが利用不可の場合は beta をボラティリティ代理変数として使用。
    risk\\_adjusted\\_score = composite \\div PENALTY\\_HIGH\\_VOL (高ボラ時)
    risk\\_adjusted\\_score = composite \\Times PENALTY\\_LOW\\_VOL (低ボラ/低流動性時)

    Args:
        ticker:          銘柄コード（ログ用）
        composite_score: 元のcomposite_score
        data:            ticker データ辞書（JP: _fetch_ticker_data() 返り値 / US: candidate dict）
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


def calc_revision_score(
    ticker: str,
    data:   dict,
    market: str,
    cfg=None,
) -> float:
    """
    FE-2: アナリスト修正スコアを算出する（JP/US共通・U-3で移設）。

    直近30日のupgrade/downgrade集計が理想だが、
    yfinance の info から取得できる rec_mean を主要代理変数として使用する。
    revision\\_score \\in [-0.5, 0.5]（quality\\_score加算前のスコア）

    市場別の倍率・上限は `config.MARKET_PROFILE` から取得する:
      - JP: 倍率6.0 / 上限 min(PEAD_TRIGGER_MIN_REVISION*10.0, 3.0)（現行実装の再現）
      - US: 倍率10.0 / 上限±5.0
    `market` が "JP"/"US" 以外の場合は JP 相当にフォールバックする。

    Args:
        ticker: 銘柄コード（ログ用）
        data:   ticker データ辞書（"rec_mean" キーを参照）
        market: "JP" または "US"
        cfg:    config モジュール（None 可）

    Returns:
        float: quality_score への加算点
    """
    try:
        window = int(getattr(cfg, "ANALYST_REVISION_WINDOW_DAYS", 30))  # noqa: F841
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

        selected_market, params = _market_revision_profile(cfg, market)
        cap = float(params["revision_cap"])
        if selected_market == "JP":
            # 旧JP実装の動的capを維持する。
            cap = min(
                float(getattr(cfg, "PEAD_TRIGGER_MIN_REVISION", 0.3)) * 10.0,
                cap,
            )
        multiplier = float(params["revision_multiplier"])
        return round(min(cap, max(-cap, raw * multiplier)), 2)

    except Exception:
        return 0.0


def check_pead_trigger(
    ticker:         str,
    revision_score: float,
    cfg=None,
) -> bool:
    """
    FE-2: PEAD（Post-Earnings Announcement Drift）トリガー判定（JP/US共通・U-3で移設）。

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


def calc_catalyst_bonus(
    ticker:      str,
    sector_code: str,
    cat_data:    dict,
    cfg=None,
) -> float:
    """
    FE-7: カタリストカレンダーからボーナス（最大 +5pt）を算出する（JP/US共通・U-3で移設）。

    cat_data は各スクリーナーがループ外で一括ロードした
    catalyst_calendar.json の内容を渡す。US 側は sector_code に
    GICS セクターコード（"US_45" 等）を渡す。

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
