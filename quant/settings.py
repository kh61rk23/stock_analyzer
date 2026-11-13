# ==============================================================
# quant/settings.py — 検証ラボ（quant/）の設定の入口
# ==============================================================
# 【役割】quant/ 配下の全モジュールは、設定値を必ず get(cfg, 名前) で読む。
#   config.py の BLOCK 82（QUANT_*）が正本で、DEFAULTS はその写し。cfg が None（単体テスト・CLI の単独実行）や
#   古い config でも同じ値で動くようにするための予備で、tests/test_quant_settings.py が両者の一致を検査する。
# 【単位】率は小数（0.01＝1%）・bps は 1/10000・日数は営業日。金利とコストは年率。
# ==============================================================
from __future__ import annotations

DEFAULTS: dict = {
    # ── 動かすか ──
    "ENABLE_QUANT_LAB": True,
    "ALERT_ON_QUANT_LAB": True,             # 日次メールに「検証ラボ」の節を出す
    # ── データ ──
    "QUANT_HISTORY_START": "2006-01-01",
    "QUANT_FFILL_LIMIT_DAYS": 3,            # 休場の差を埋める前方補完の上限（これを超える欠けは欠損のまま）
    "QUANT_MIN_HISTORY_DAYS": 756,          # これより短い銘柄は戦略の対象から外す（3年）
    "QUANT_CACHE_MAX_AGE_HOURS": 18,
    # ── 対象（symbol は yfinance のティッカー。exec は実際に建てるときの商品の目安） ──
    # funding: cash=現物（借りない）／margin_jp・margin_us=信用（買いは買方金利・売りは貸株料）／
    #          fx=金利差（スワップ）／futures=先物・CFD（現金の金利を引いた超過収益で測る）
    "QUANT_UNIVERSE": {
        "fx": [
            {"symbol": "USDJPY=X", "name": "米ドル/円", "base": "USD", "quote": "JPY", "cost_bps": 1.0},
            {"symbol": "EURJPY=X", "name": "ユーロ/円", "base": "EUR", "quote": "JPY", "cost_bps": 1.5},
            {"symbol": "GBPJPY=X", "name": "英ポンド/円", "base": "GBP", "quote": "JPY", "cost_bps": 2.0},
            {"symbol": "AUDJPY=X", "name": "豪ドル/円", "base": "AUD", "quote": "JPY", "cost_bps": 1.5},
            {"symbol": "NZDJPY=X", "name": "NZドル/円", "base": "NZD", "quote": "JPY", "cost_bps": 2.0},
            {"symbol": "CADJPY=X", "name": "カナダドル/円", "base": "CAD", "quote": "JPY", "cost_bps": 2.0},
            {"symbol": "CHFJPY=X", "name": "スイスフラン/円", "base": "CHF", "quote": "JPY", "cost_bps": 2.0},
            {"symbol": "EURUSD=X", "name": "ユーロ/米ドル", "base": "EUR", "quote": "USD", "cost_bps": 1.0},
            {"symbol": "GBPUSD=X", "name": "英ポンド/米ドル", "base": "GBP", "quote": "USD", "cost_bps": 1.5},
            {"symbol": "AUDUSD=X", "name": "豪ドル/米ドル", "base": "AUD", "quote": "USD", "cost_bps": 1.5},
        ],
        # 先物の期近連続足（GC=F 等）は限月の乗り換えで値が跳び、持っていても得られない損益が混ざる。
        # 検証は乗り換えの損益を基準価額に含む ETF で行い、建てるときの先物・CFD を exec に書く。
        "commodity": [
            {"symbol": "GLD", "name": "金", "exec": "金先物・金CFD（GC）", "cost_bps": 4.0},
            {"symbol": "SLV", "name": "銀", "exec": "銀先物・銀CFD（SI）", "cost_bps": 6.0},
            {"symbol": "PPLT", "name": "プラチナ", "exec": "白金先物（PL）", "cost_bps": 8.0},
            {"symbol": "CPER", "name": "銅", "exec": "銅先物・銅CFD（HG）", "cost_bps": 8.0},
            {"symbol": "USO", "name": "原油（WTI）", "exec": "原油先物・原油CFD（CL）", "cost_bps": 6.0},
            {"symbol": "UNG", "name": "天然ガス", "exec": "天然ガス先物・CFD（NG）", "cost_bps": 8.0},
            {"symbol": "DBA", "name": "農産物（総合）", "exec": "穀物・ソフトの先物の束", "cost_bps": 8.0},
            {"symbol": "CORN", "name": "とうもろこし", "exec": "とうもろこし先物・CFD（ZC）", "cost_bps": 10.0},
            {"symbol": "WEAT", "name": "小麦", "exec": "小麦先物（ZW）", "cost_bps": 10.0},
            {"symbol": "SOYB", "name": "大豆", "exec": "大豆先物・CFD（ZS）", "cost_bps": 10.0},
            {"symbol": "DBB", "name": "非鉄金属（総合）", "exec": "銅・アルミ・亜鉛の先物の束", "cost_bps": 8.0},
            {"symbol": "DBC", "name": "商品（総合）", "exec": "商品指数の先物・ETF", "cost_bps": 6.0},
        ],
        "index": [
            {"symbol": "1321.T", "name": "日経225", "exec": "日経225先物ミニ・マイクロ／ETF 1321", "cost_bps": 3.0, "currency": "JPY"},
            {"symbol": "1306.T", "name": "TOPIX", "exec": "TOPIX先物ミニ／ETF 1306", "cost_bps": 3.0, "currency": "JPY"},
            {"symbol": "SPY", "name": "S&P500", "exec": "S&P500 先物・CFD／ETF", "cost_bps": 2.0},
            {"symbol": "QQQ", "name": "NASDAQ100", "exec": "NASDAQ100 先物・CFD／ETF", "cost_bps": 2.0},
            {"symbol": "EFA", "name": "先進国株（除く米国）", "exec": "ETF", "cost_bps": 4.0},
            {"symbol": "EEM", "name": "新興国株", "exec": "ETF", "cost_bps": 4.0},
            {"symbol": "TLT", "name": "米国長期国債", "exec": "米国債先物／ETF", "cost_bps": 3.0},
            {"symbol": "IEF", "name": "米国中期国債", "exec": "米国債先物／ETF", "cost_bps": 3.0},
        ],
        "jp_margin": [
            {"symbol": f"{c}.T", "name": n, "exec": f"信用取引（ETF {c}・貸借銘柄かは発注前に確認）", "cost_bps": 15.0}
            for c, n in (
                (1617, "食品"), (1618, "エネルギー資源"), (1619, "建設・資材"), (1620, "素材・化学"),
                (1621, "医薬品"), (1622, "自動車・輸送機"), (1623, "鉄鋼・非鉄"), (1624, "機械"),
                (1625, "電機・精密"), (1626, "情報通信・サービス"), (1627, "電力・ガス"), (1628, "運輸・物流"),
                (1629, "商社・卸売"), (1630, "小売"), (1631, "銀行"), (1632, "金融（除く銀行）"), (1633, "不動産"),
            )
        ],
        "us_margin": [
            {"symbol": s, "name": n, "exec": f"米国株信用（ETF {s}）", "cost_bps": 3.0}
            for s, n in (
                ("XLB", "素材"), ("XLE", "エネルギー"), ("XLF", "金融"), ("XLI", "資本財"), ("XLK", "情報技術"),
                ("XLP", "生活必需品"), ("XLU", "公益"), ("XLV", "ヘルスケア"), ("XLY", "一般消費財"),
            )
        ],
        # オプションの原資産と、その予想変動率の指数（無い・取れないときは実績の変動率×QUANT_OPT_IV_HV_RATIO）
        "options": [
            {"symbol": "SPY", "name": "S&P500", "iv_symbol": "^VIX", "exec": "SPY・SPX のオプション", "cost_bps": 0.0},
            {"symbol": "QQQ", "name": "NASDAQ100", "iv_symbol": "^VXN", "exec": "QQQ・NDX のオプション", "cost_bps": 0.0},
            {"symbol": "1321.T", "name": "日経225", "iv_symbol": None, "exec": "日経225オプション（ミニ）", "cost_bps": 0.0, "currency": "JPY"},
        ],
    },
    "QUANT_ASSET_CLASS_FUNDING": {
        "fx": "fx", "commodity": "futures", "index": "futures",
        "jp_margin": "margin_jp", "us_margin": "margin_us", "options": "cash",
    },
    "QUANT_ASSET_CLASS_LABEL": {
        "fx": "FX", "commodity": "コモディティ", "index": "株価指数・債券", "jp_margin": "信用取引（日本）",
        "us_margin": "信用取引（米国）", "options": "オプション", "equity": "現物株（既存の仮想トレード）",
        "ensemble": "組み合わせ",
    },
    # ── 金利（年率・小数）。BIS の政策金利（日次・キー不要の一括ファイル）を使う ──
    "QUANT_RATE_BIS_URL": "https://data.bis.org/static/bulk/WS_CBPOL_csv_flat.zip",
    "QUANT_RATE_BIS_AREAS": {"USD": "US", "JPY": "JP", "EUR": "XM", "GBP": "GB", "AUD": "AU", "NZD": "NZ",
                             "CAD": "CA", "CHF": "CH"},
    # 取れないときの値（履歴が無いので、金利の履歴が要る戦略は「金利の履歴なし」として検証の対象から外す）
    "QUANT_RATE_FALLBACK": {"USD": 0.03875, "JPY": 0.0125, "EUR": 0.025, "GBP": 0.040, "AUD": 0.046,
                            "NZD": 0.0275, "CAD": 0.025, "CHF": 0.0},
    "QUANT_FX_SWAP_MARKUP": 0.0075,          # 業者の取り分（買いでも売りでも金利差から引く・年率）
    "QUANT_MARGIN_JP_LONG_RATE": 0.028,      # 制度信用の買方金利
    "QUANT_MARGIN_JP_SHORT_RATE": 0.0115,    # 貸株料
    "QUANT_MARGIN_JP_SHORT_PREMIUM": 0.010,  # 逆日歩の見込み（毎日は付かないので年率でならした額）
    "QUANT_MARGIN_US_LONG_RATE_SPREAD": 0.025,   # 米ドル金利に上乗せ
    "QUANT_MARGIN_US_SHORT_RATE": 0.010,
    # ── レバレッジの上限（建玉の合計÷資金）。法令の上限（FX 25倍・信用 約3.3倍）より低く置く ──
    "QUANT_MAX_LEVERAGE": {"fx": 3.0, "commodity": 1.5, "index": 1.5, "jp_margin": 2.0, "us_margin": 1.5,
                           "options": 1.0, "equity": 1.0},
    # ── 戦略の作り ──
    "QUANT_VOL_LOOKBACK": 60,
    "QUANT_STRATEGY_TARGET_VOL": 0.10,       # 戦略ごとの年率の変動の目標
    "QUANT_REBALANCE_BAND": 0.10,            # 目標との差がこの割合（相対）未満なら建玉を動かさない
    # ── オプションの検証（理論価格で値を付ける） ──
    "QUANT_OPT_IV_INDEX_RATIO": 0.88,        # 予想変動率の指数（VIX 等）×この値＝現値の行使価格の予想変動率（指数は裾を含むぶん高い）
    "QUANT_OPT_IV_HV_RATIO": 1.10,           # 予想変動率の指数が無いとき：実績の変動率×この値
    "QUANT_OPT_SKEW_PER_10PCT": 0.25,        # 行使価格が現値より10%下がるごとに予想変動率を相対でこれだけ上げる（プット側の割高）
    "QUANT_OPT_CALL_SKEW_PER_10PCT": 0.40,   # 行使価格が現値より10%上がるごとに予想変動率を相対でこれだけ下げる（コール側は割安）
    "QUANT_OPT_SPREAD_PCT": 0.06,            # 売値と買値の差（代金に対する率）。片道でこの半分を払う
    "QUANT_OPT_MIN_COST_PER_SHARE": 0.01,
    "QUANT_OPT_DTE": 30,
    "QUANT_OPT_MODEL_HAIRCUT": 0.70,         # 理論価格の検証なので、シャープに掛けて割り引いてから門に通す
    # ── 検証（歩いて進む最適化） ──
    "QUANT_WFO_FOLDS": 6,
    "QUANT_WFO_MIN_TRAIN_DAYS": 756,
    "QUANT_WFO_EMBARGO_DAYS": 21,
    "QUANT_BOOT_N": 2000,
    "QUANT_BOOT_MEAN_BLOCK": 20,
    "QUANT_BOOT_SEED": 20260101,
    "QUANT_PBO_BLOCKS": 10,
    "QUANT_COST_STRESS_MULT": 2.0,
    # ── あらかじめ決めた組み合わせ（個々の戦略と同じ門に通す。成績を見てから組を選ばない） ──
    # members は family_id の型（fnmatch）。配分は各戦略の過去の変動の逆数（前日までの値）。既存の仮想トレードは入れない
    "QUANT_ENSEMBLES": {
        "ens_all": {"label": "全戦略の組み合わせ", "members": ("*",)},
        "ens_trend": {"label": "順張りの組み合わせ（FX・コモディティ・指数）",
                      "members": ("fx_tsmom", "cmd_tsmom", "idx_trend", "multi_tsmom")},
        "ens_premium": {"label": "オプションの売りの組み合わせ", "members": ("opt_*",)},
        "ens_margin": {"label": "信用取引の組み合わせ（日本・米国）", "members": ("jpm_*", "usm_*")},
        "ens_fx": {"label": "FX の組み合わせ（順張り・金利差）", "members": ("fx_*",)},
    },
    "QUANT_ENSEMBLE_VOL_LOOKBACK": 126,
    "QUANT_ENSEMBLE_MIN_MEMBERS": 2,
    # ── 採用の門（全部を満たしたものだけ採用） ──
    "QUANT_GATE_MIN_OOS_DAYS": 750,
    "QUANT_GATE_MIN_OOS_SHARPE": 0.50,
    "QUANT_GATE_MIN_DSR": 0.95,              # 試した数を割り引いた後で、本当のシャープが0を超える確率
    "QUANT_GATE_MAX_PBO": 0.25,              # 過剰適合の確率
    "QUANT_GATE_MAX_ADJ_P": 0.05,            # 全候補で Holm 補正した後の p 値
    "QUANT_GATE_MIN_P_PROFIT_12M": 0.70,     # 12か月後に利益が出ている確率（ブロック・ブートストラップ）
    "QUANT_GATE_MIN_STRESS_SHARPE": 0.20,    # コストを QUANT_COST_STRESS_MULT 倍にしたときのシャープ
    "QUANT_GATE_MAX_DRAWDOWN": 0.25,
    "QUANT_GATE_MIN_FOLD_POSITIVE": 0.60,    # 検証区間のうち利益が出た区間の割合
    "QUANT_GATE_WATCH_MAX_FAILS": 2,
    # 全項目を満たしてもすぐには採用しない。満たした日より後の検証の損益（設計を見た後の前向きの実績）がこの営業日数たまり、
    # 見張りの基準（monitor）で悪化していなければ採用する。途中で項目を落としたら数え直す
    "QUANT_PROBATION_DAYS": 60,         # 落ちた項目がこの数以下で、必須の項目を通っていれば「観察」
    "QUANT_GATE_CRITICAL": ("oos_days", "oos_sharpe", "stress_sharpe"),
    # ── 組み合わせ（採用した戦略の配分） ──
    "QUANT_PORT_TARGET_VOL": 0.08,
    "QUANT_PORT_MAX_STRATEGY_WEIGHT": 0.35,
    "QUANT_PORT_MAX_CLASS_WEIGHT": 0.50,
    "QUANT_PORT_KELLY_FRACTION": 0.25,
    "QUANT_PORT_MAX_SCALE": 1.5,             # 変動の目標に合わせるときの倍率の上限
    "QUANT_PORT_DD_BRAKES": ((0.08, 0.5), (0.12, 0.0)),   # （高値からの下げ, 建玉に掛ける倍率）
    "QUANT_PORT_HORIZONS": (63, 126, 252),
    # ── 採用後の見張り ──
    "QUANT_MONITOR_MIN_DAYS": 40,
    "QUANT_MONITOR_Q": 0.05,                 # 実績が検証の分布のこの分位を下回ったら停止
    "QUANT_MONITOR_DD_MULT": 1.5,            # 実績の下げが検証の最大の下げ×この値を超えたら停止
    "QUANT_REGISTRY_MAX_AGE_DAYS": 21,       # 検証結果がこれより古いときは提案を出さない
    # ── 置き場（DRIVE_BASE からの相対） ──
    "QUANT_DIR": "quant",
    "QUANT_CACHE_DIR": "cache/quant",
    "QUANT_INSIGHT_DIR": "insights/quant",
}


def get(cfg, name: str):
    """設定値を返す。cfg に無ければ DEFAULTS。DEFAULTS にも無い名前は KeyError（つづりの誤りを黙って通さない）。"""
    if name not in DEFAULTS:
        raise KeyError(f"quant の設定に無い名前: {name}")
    if cfg is not None and hasattr(cfg, name):
        return getattr(cfg, name)
    return DEFAULTS[name]


def base_dir(cfg):
    """状態・キャッシュの親（config.DRIVE_BASE。cfg が無ければ ./stock_data）。"""
    from pathlib import Path
    return Path(getattr(cfg, "DRIVE_BASE", "stock_data")) if cfg is not None else Path("stock_data")


def path(cfg, name: str):
    """QUANT_DIR 等の置き場を絶対化して返す（無ければ作る）。"""
    p = base_dir(cfg) / get(cfg, name)
    p.mkdir(parents=True, exist_ok=True)
    return p
