# ==============================================================
# ignition_detector.py  v1.0  (Wave 2 - FE-4)
# Ignition Detector: カテゴリA/B/Cシグナル収束検出エンジン
# ==============================================================
# 【役割】
#   カテゴリA（資金フロー）/ B（ナラティブ）/ C（価格）の3シグナルが
#   IGNITION_CONVERGENCE_WINDOW_DAYS（デフォルト14日）以内に
#   同一セクター / テーマに収束したことを検出し、収束スコアを算出する。
#
# 【収束スコア（convergence_score）設計】
#   $convergence\_score = (w_A \times s_A + w_B \times s_B + w_C \times s_C) \times 5$
#   s_A, s_B, s_C : 各カテゴリのシグナル強度（0.0〜1.0）
#   w_A=0.40 / w_B=0.35 / w_C=0.25（config.py BLOCK 30 で上書き可能）
#   最大値 = 5.0
#
# 【LINE通知閾値】
#   $convergence\_score \geq 3.0$ → LINE即時通知（alert_notifier.py トリガー㉑）
#   $convergence\_score \geq 3.5$ かつ14日以内2回以上 → 最高警戒LINE
#
# 【カテゴリ定義】
#   Category A（資金フロー）: signal_aggregator money_flow / 機関投資家保有率 / ローテーション
#   Category B（ナラティブ）: arxiv論文数 / アナリスト修正score / マクロ追い風シグナル数
#   Category C（価格）      : セクター平均composite_score / 52週騰落率 / リスク調整後スコア
#
# 【生成キャッシュ】
#   ignition_alerts.json   TTL: 14日
#   ignition_history.json  TTL: 180日
#
# 【呼び出しパターン（stock_analyzer_main.py から）】
#   from ignition_detector import run_ignition_detector
#   ign_result = run_ignition_detector(cfg, results=results)
#   results["ignition"] = ign_result
#
# 【依存】
#   config.py (BLOCK 30 定数参照)
#   追加ライブラリなし（標準ライブラリのみ）
#   ★ 全関数を try/except で保護。失敗時はデフォルト値を返す。
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: デフォルト定数（config.py BLOCK 30 で上書き可能）
# ==============================================================

_DEFAULT_CONVERGENCE_WINDOW  = 14      # シグナル収束ウィンドウ（日）
_DEFAULT_MIN_SCORE_LINE      = 3.0    # LINE通知閾値
_DEFAULT_CRITICAL_SCORE      = 3.5    # 最高警戒LINE閾値
_DEFAULT_CRITICAL_REPEAT     = 14     # 最高警戒反復判定日数
_DEFAULT_ALERT_TTL           = 14     # ignition_alerts.json TTL（日）
_DEFAULT_HISTORY_TTL         = 180    # ignition_history.json TTL（日）

_DEFAULT_WEIGHT_A = 0.40   # Category A（資金フロー）
_DEFAULT_WEIGHT_B = 0.35   # Category B（ナラティブ）
_DEFAULT_WEIGHT_C = 0.25   # Category C（価格）

# Category 検出閾値（各スコアがこの値以上で "detected" フラグが立つ）
_CAT_DETECT_THRESHOLD = 0.30

# ==============================================================
# BLOCK 1: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def _load_json_cache(filename: str, cfg=None) -> dict | list:
    try:
        p = _cache_dir(cfg) / filename
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_cache(filename: str, data: dict | list, cfg=None) -> None:
    try:
        p = _cache_dir(cfg) / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_ignition_alerts(cfg=None) -> dict:
    """ignition_alerts.json を読み込む。TTL切れは空dictを返す。"""
    try:
        raw = _load_json_cache("ignition_alerts.json", cfg)
        if not raw or not isinstance(raw, dict):
            return {}
        ttl = int(getattr(cfg, "IGNITION_ALERT_TTL_DAYS", _DEFAULT_ALERT_TTL))
        gen  = raw.get("generated", "")
        if gen:
            cutoff = (datetime.now() - timedelta(days=ttl)).strftime("%Y-%m-%d")
            if gen < cutoff:
                return {}
        return raw
    except Exception:
        return {}


def _save_ignition_alerts(alerts: dict, cfg=None) -> None:
    """ignition_alerts.json を保存する。"""
    _save_json_cache("ignition_alerts.json", alerts, cfg)


def _load_ignition_history(cfg=None) -> list:
    """ignition_history.json を読み込む。180日超のエントリは除外する。"""
    try:
        raw = _load_json_cache("ignition_history.json", cfg)
        if not isinstance(raw, list):
            raw = raw.get("history", []) if isinstance(raw, dict) else []
        ttl    = int(getattr(cfg, "IGNITION_HISTORY_TTL_DAYS", _DEFAULT_HISTORY_TTL))
        cutoff = (datetime.now() - timedelta(days=ttl)).strftime("%Y-%m-%d")
        return [
            e for e in raw
            if isinstance(e, dict) and e.get("detected_at", "") >= cutoff
        ]
    except Exception:
        return []


def _save_ignition_history(history: list, cfg=None) -> None:
    """ignition_history.json を保存する。"""
    _save_json_cache("ignition_history.json", history, cfg)


# ==============================================================
# BLOCK 2: データバンドル構築
# ==============================================================

def _build_data_bundle(cfg=None, results: Optional[dict] = None) -> dict:
    """
    各モジュールの結果から ignition 判定に必要なデータを収集する。

    Args:
        cfg:     config モジュール
        results: stock_analyzer_main.py の results 辞書（None 可）

    Returns:
        {
          "screener_candidates": list,   # 全通過銘柄
          "sector_results":      dict,   # {sector_code: base_agent_result}
          "signal_agg":          dict,   # {sector_code: signal_aggregator_result}
          "sector_rotation":     dict,   # sector_rotation_detector 結果
          "arxiv_papers":        list,   # arxiv 論文リスト
        }
    """
    bundle: dict = {
        "screener_candidates": [],
        "sector_results":      {},
        "signal_agg":          {},
        "sector_rotation":     {},
        "arxiv_papers":        [],
    }

    if results is None:
        results = {}

    try:
        # ── screener 通過銘柄 ─────────────────────────────────
        screener = results.get("screener", {})
        bundle["screener_candidates"] = (
            screener.get("passed")
            or screener.get("candidates", [])
        )
    except Exception:
        pass

    try:
        # ── セクター分析結果（base_agent） ────────────────────
        bundle["sector_results"] = dict(results.get("sectors", {}))
    except Exception:
        pass

    try:
        # ── signal_aggregator 結果 ────────────────────────────
        bundle["signal_agg"] = dict(results.get("signal_aggregator", {}))
    except Exception:
        pass

    try:
        # ── sector_rotation 結果 ─────────────────────────────
        bundle["sector_rotation"] = dict(results.get("sector_rotation", {}))
    except Exception:
        pass

    try:
        # ── arxiv 論文（screener or 専用キャッシュから取得） ──
        arxiv_raw = results.get("arxiv", {})
        bundle["arxiv_papers"] = list(arxiv_raw.get("papers", []))
    except Exception:
        pass

    return bundle


def _safe_mean(values: list) -> Optional[float]:
    """None を除いたリストの平均値を返す。"""
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


# ==============================================================
# BLOCK 3: カテゴリ別シグナル抽出
# ==============================================================

def _extract_category_a(
    sector_code: str,
    bundle:      dict,
    cfg=None,
) -> dict:
    """
    Category A（資金フロー）シグナルを抽出する。

    【評価指標】
    1. signal_aggregator の money_flow コンポーネント（>60pt → +0.5）
    2. screener通過銘柄の機関投資家保有率平均（>60% → +0.3）
    3. sector_rotation の rotation_score（>70 → +0.2）

    Returns:
        {"score": 0.0〜1.0, "signals": list[str], "detected": bool, "timestamp": str}
    """
    score   = 0.0
    signals: list[str] = []

    try:
        # ── 指標1: signal_aggregator money_flow ──────────────
        sig_agg   = bundle.get("signal_agg", {})
        sec_sig   = sig_agg.get(str(sector_code), {})
        components = sec_sig.get("components", {})
        money_flow = components.get("money_flow") or components.get("sector_score")
        if money_flow is not None:
            mf = float(money_flow)
            if mf >= 70:
                score   += 0.50
                signals.append(f"資金フロー強({mf:.0f}pt)")
            elif mf >= 55:
                score   += 0.30
                signals.append(f"資金フロー中({mf:.0f}pt)")
            elif mf >= 40:
                score   += 0.10
    except Exception:
        pass

    try:
        # ── 指標2: 機関投資家保有率平均 ──────────────────────
        candidates = bundle.get("screener_candidates", [])
        sector_cands = [
            c for c in candidates
            if str(c.get("sector_code", "")) == str(sector_code)
        ]
        avg_inst = _safe_mean([c.get("inst_ownership") for c in sector_cands])
        if avg_inst is not None:
            if avg_inst >= 0.65:
                score   += 0.30
                signals.append(f"機関保有率高({avg_inst*100:.0f}%)")
            elif avg_inst >= 0.50:
                score   += 0.15
    except Exception:
        pass

    try:
        # ── 指標3: sector_rotation_score ─────────────────────
        rotation  = bundle.get("sector_rotation", {})
        sec_rot   = rotation.get("sectors", {}).get(str(sector_code), {})
        rot_score = sec_rot.get("rotation_score")
        if rot_score is not None:
            rs = float(rot_score)
            if rs >= 75:
                score   += 0.20
                signals.append(f"ローテーション強({rs:.0f}pt)")
            elif rs >= 60:
                score   += 0.10
                signals.append(f"ローテーション中({rs:.0f}pt)")
    except Exception:
        pass

    score = min(1.0, score)
    return {
        "score":     round(score, 3),
        "signals":   signals,
        "detected":  score >= _CAT_DETECT_THRESHOLD,
        "timestamp": datetime.now().isoformat(),
    }


def _extract_category_b(
    sector_code: str,
    bundle:      dict,
    cfg=None,
) -> dict:
    """
    Category B（ナラティブ）シグナルを抽出する。

    【評価指標】
    1. arxiv 論文数（≥3本 → +0.40 / ≥1本 → +0.20）
    2. screener通過銘柄の平均 revision_score（≥0.3 → +0.35）
    3. macro 追い風シグナル数（≥2件 → +0.25 / 1件 → +0.10）

    Returns:
        {"score": 0.0〜1.0, "signals": list[str], "detected": bool, "timestamp": str}
    """
    score   = 0.0
    signals: list[str] = []

    try:
        # ── 指標1: arxiv 論文数 ──────────────────────────────
        papers = bundle.get("arxiv_papers", [])
        sector_papers = [
            p for p in papers
            if str(sector_code) in (p.get("sectors") or p.get("sector_codes", []))
        ]
        n = len(sector_papers)
        if n >= 3:
            score   += 0.40
            signals.append(f"ArXiv論文{n}本（テーマ集中）⭐")
        elif n >= 1:
            score   += 0.20
            signals.append(f"ArXiv論文{n}本")
    except Exception:
        pass

    try:
        # ── 指標2: アナリスト修正スコア ──────────────────────
        min_rev  = float(getattr(cfg, "PEAD_TRIGGER_MIN_REVISION", 0.3))
        candidates = bundle.get("screener_candidates", [])
        sector_cands = [
            c for c in candidates
            if str(c.get("sector_code", "")) == str(sector_code)
        ]
        avg_rev = _safe_mean([c.get("revision_score") for c in sector_cands])
        if avg_rev is not None:
            if avg_rev >= min_rev:
                score   += 0.35
                signals.append(f"アナリスト修正スコア+{avg_rev:.2f}pt⭐")
            elif avg_rev >= min_rev * 0.5:
                score   += 0.15
                signals.append(f"アナリスト修正スコア+{avg_rev:.2f}pt")
    except Exception:
        pass

    try:
        # ── 指標3: マクロ追い風シグナル数 ────────────────────
        sector_res = bundle.get("sector_results", {}).get(str(sector_code), {})
        macro_sigs = sector_res.get("macro_signals", [])
        tailwinds  = [s for s in macro_sigs if s.get("signal") == "tailwind"]
        n_tw = len(tailwinds)
        if n_tw >= 2:
            score   += 0.25
            signals.append(f"マクロ追い風{n_tw}件⭐")
        elif n_tw == 1:
            score   += 0.10
            signals.append(f"マクロ追い風1件")
    except Exception:
        pass

    score = min(1.0, score)
    return {
        "score":     round(score, 3),
        "signals":   signals,
        "detected":  score >= _CAT_DETECT_THRESHOLD,
        "timestamp": datetime.now().isoformat(),
    }


def _extract_category_c(
    sector_code: str,
    bundle:      dict,
    cfg=None,
) -> dict:
    """
    Category C（価格）シグナルを抽出する。

    【評価指標】
    1. セクター平均 composite_score（>70pt → +0.50 / >55pt → +0.30）
    2. セクター平均 week52_change（>15% → +0.30 / >5% → +0.15）
    3. セクター平均 risk_adjusted_score（>65pt → +0.20）

    Returns:
        {"score": 0.0〜1.0, "signals": list[str], "detected": bool, "timestamp": str}
    """
    score   = 0.0
    signals: list[str] = []

    try:
        candidates = bundle.get("screener_candidates", [])
        sector_cands = [
            c for c in candidates
            if str(c.get("sector_code", "")) == str(sector_code)
        ]

        if not sector_cands:
            return {
                "score": 0.0, "signals": ["スクリーナー通過銘柄なし"],
                "detected": False, "timestamp": datetime.now().isoformat(),
            }

        # ── 指標1: 平均 composite_score ──────────────────────
        avg_comp = _safe_mean([c.get("composite_score") for c in sector_cands])
        if avg_comp is not None:
            if avg_comp >= 70:
                score   += 0.50
                signals.append(f"セクタースコア高({avg_comp:.0f}pt)⭐")
            elif avg_comp >= 55:
                score   += 0.30
                signals.append(f"セクタースコア中({avg_comp:.0f}pt)")
            elif avg_comp >= 45:
                score   += 0.10

        # ── 指標2: 平均 52週騰落率 ───────────────────────────
        avg_w52 = _safe_mean([c.get("week52_change") for c in sector_cands])
        if avg_w52 is not None:
            if avg_w52 >= 0.15:
                score   += 0.30
                signals.append(f"52週騰落+{avg_w52*100:.0f}%⭐")
            elif avg_w52 >= 0.05:
                score   += 0.15
                signals.append(f"52週騰落+{avg_w52*100:.0f}%")
            elif avg_w52 < -0.15:
                # 大幅下落 → 逆張りシグナルとして弱い価格シグナル
                score   += 0.05

        # ── 指標3: 平均 risk_adjusted_score ─────────────────
        avg_ras = _safe_mean([
            c.get("risk_adjusted_score", c.get("composite_score"))
            for c in sector_cands
        ])
        if avg_ras is not None and avg_ras >= 65:
            score   += 0.20
            signals.append(f"リスク調整後スコア{avg_ras:.0f}pt")

    except Exception:
        pass

    score = min(1.0, score)
    return {
        "score":     round(score, 3),
        "signals":   signals,
        "detected":  score >= _CAT_DETECT_THRESHOLD,
        "timestamp": datetime.now().isoformat(),
    }


# ==============================================================
# BLOCK 4: 収束スコア計算
# ==============================================================

def _calc_convergence_score(
    cat_a: dict,
    cat_b: dict,
    cat_c: dict,
    cfg=None,
) -> float:
    """
    カテゴリA/B/Cのスコアから収束スコアを算出する。

    $convergence\\_score = (w_A \times s_A + w_B \times s_B + w_C \times s_C) \times 5$

    最大値 = 5.0（全カテゴリ s=1.0 時）

    Args:
        cat_a: _extract_category_a() の返り値
        cat_b: _extract_category_b() の返り値
        cat_c: _extract_category_c() の返り値
        cfg:   config モジュール（None 可）

    Returns:
        float: convergence_score (0.0〜5.0)
    """
    try:
        w_a = float(getattr(cfg, "IGNITION_WEIGHT_CATEGORY_A", _DEFAULT_WEIGHT_A))
        w_b = float(getattr(cfg, "IGNITION_WEIGHT_CATEGORY_B", _DEFAULT_WEIGHT_B))
        w_c = float(getattr(cfg, "IGNITION_WEIGHT_CATEGORY_C", _DEFAULT_WEIGHT_C))

        s_a = float(cat_a.get("score", 0.0))
        s_b = float(cat_b.get("score", 0.0))
        s_c = float(cat_c.get("score", 0.0))

        raw = (w_a * s_a + w_b * s_b + w_c * s_c) * 5.0
        return round(min(5.0, max(0.0, raw)), 2)

    except Exception:
        return 0.0


# ==============================================================
# BLOCK 5: 最高警戒判定（14日以内2回以上）
# ==============================================================

def _check_critical_repeat(
    sector_code: str,
    history:     list,
    cfg=None,
) -> bool:
    """
    ignition_history から14日以内に同一セクターで2回以上の
    convergence_score ≥ IGNITION_MAX_SCORE_LINE_CRITICAL を検出したかを判定する。

    Args:
        sector_code: 判定対象セクターコード
        history:     ignition_history のリスト
        cfg:         config モジュール（None 可）

    Returns:
        bool: True → 最高警戒LINE発動条件を満たす
    """
    try:
        crit_score  = float(getattr(cfg, "IGNITION_MAX_SCORE_LINE_CRITICAL",
                                     _DEFAULT_CRITICAL_SCORE))
        repeat_days = int(getattr(cfg, "IGNITION_CRITICAL_REPEAT_DAYS",
                                   _DEFAULT_CRITICAL_REPEAT))
        cutoff = (datetime.now() - timedelta(days=repeat_days)).strftime("%Y-%m-%d")

        matches = [
            e for e in history
            if (str(e.get("sector_code", "")) == str(sector_code)
                and float(e.get("convergence_score", 0.0)) >= crit_score
                and e.get("detected_at", "") >= cutoff)
        ]
        return len(matches) >= 2  # 今回も含めると2回以上

    except Exception:
        return False


# ==============================================================
# BLOCK 6: セクター別収束判定
# ==============================================================

def detect_convergence_for_sector(
    sector_code: str,
    sector_name: str,
    bundle:      dict,
    history:     list,
    cfg=None,
) -> dict:
    """
    1セクターに対して収束判定を実行し、結果辞書を返す。

    Returns:
        {
          "sector_code":       str,
          "sector_name":       str,
          "convergence_score": float,   # 0.0〜5.0
          "category_a":        dict,
          "category_b":        dict,
          "category_c":        dict,
          "is_ignition":       bool,    # convergence_score ≥ MIN_SCORE_LINE
          "is_critical":       bool,    # ≥ CRITICAL_SCORE かつ反復
          "detected_at":       str,     # ISO 8601
          "ttl_expiry":        str,     # YYYY-MM-DD
        }
    """
    try:
        cat_a = _extract_category_a(sector_code, bundle, cfg)
        cat_b = _extract_category_b(sector_code, bundle, cfg)
        cat_c = _extract_category_c(sector_code, bundle, cfg)

        conv_score = _calc_convergence_score(cat_a, cat_b, cat_c, cfg)

        min_score  = float(getattr(cfg, "IGNITION_MIN_SCORE_LINE",
                                    _DEFAULT_MIN_SCORE_LINE))
        crit_score = float(getattr(cfg, "IGNITION_MAX_SCORE_LINE_CRITICAL",
                                    _DEFAULT_CRITICAL_SCORE))
        alert_ttl  = int(getattr(cfg, "IGNITION_ALERT_TTL_DAYS", _DEFAULT_ALERT_TTL))

        is_ignition = conv_score >= min_score
        is_crit_pre = conv_score >= crit_score

        # 最高警戒: 現時点ですでに history に過去1回以上あれば合計2回以上
        is_critical = is_crit_pre and _check_critical_repeat(
            sector_code, history, cfg
        )

        ttl_expiry = (
            datetime.now() + timedelta(days=alert_ttl)
        ).strftime("%Y-%m-%d")

        return {
            "sector_code":       sector_code,
            "sector_name":       sector_name,
            "convergence_score": conv_score,
            "category_a":        cat_a,
            "category_b":        cat_b,
            "category_c":        cat_c,
            "is_ignition":       is_ignition,
            "is_critical":       is_critical,
            "detected_at":       datetime.now().isoformat(),
            "ttl_expiry":        ttl_expiry,
        }

    except Exception as e:
        return {
            "sector_code":       sector_code,
            "sector_name":       sector_name,
            "convergence_score": 0.0,
            "category_a":        {"score": 0.0, "signals": [], "detected": False},
            "category_b":        {"score": 0.0, "signals": [], "detected": False},
            "category_c":        {"score": 0.0, "signals": [], "detected": False},
            "is_ignition":       False,
            "is_critical":       False,
            "detected_at":       datetime.now().isoformat(),
            "ttl_expiry":        "",
            "error":             str(e)[:80],
        }


# ==============================================================
# BLOCK 7: メインエントリポイント
# ==============================================================

def run_ignition_detector(cfg=None, results: Optional[dict] = None) -> dict:
    """
    Ignition Detector のメインエントリポイント。
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれる。

    処理フロー:
      1. データバンドルを構築（screener / signal_agg / sectors / arxiv）
      2. JP_SECTORS の全セクターに対して収束判定を実行
      3. convergence_score ≥ 閾値のセクターを alerts に格納
      4. ignition_alerts.json / ignition_history.json を更新
      5. サマリーを返す

    Args:
        cfg:     config モジュール（None 可）
        results: stock_analyzer_main.py の results 辞書（None 可）

    Returns:
        {
          "alerts":          list,   # 発火セクターのリスト
          "all_results":     dict,   # {sector_code: 判定結果}
          "ignition_count":  int,    # 発火セクター数
          "critical_count":  int,    # 最高警戒セクター数
          "summary":         str,    # ログ用サマリー文字列
          "generated":       str,    # 実行日時
        }
    """
    print("  🔥 [ignition_detector] 収束シグナル検出開始...")

    # ── ① データバンドル構築 ──────────────────────────────────
    try:
        bundle = _build_data_bundle(cfg, results)
    except Exception as e:
        print(f"  ⚠️  [ignition_detector] データバンドル構築エラー: {str(e)[:60]}")
        bundle = {
            "screener_candidates": [],
            "sector_results":      {},
            "signal_agg":          {},
            "sector_rotation":     {},
            "arxiv_papers":        [],
        }

    # ── ② 対象セクターリスト取得 ──────────────────────────────
    try:
        jp_sectors: dict = getattr(cfg, "JP_SECTORS", {})
        if not jp_sectors:
            # cfg なし / 空の場合は screener 結果から推定
            cands = bundle.get("screener_candidates", [])
            jp_sectors = {
                str(c.get("sector_code", "")): c.get("sector_name", "")
                for c in cands
                if c.get("sector_code")
            }
    except Exception:
        jp_sectors = {}

    # ── ③ 既存履歴を読み込む ─────────────────────────────────
    try:
        history = _load_ignition_history(cfg)
    except Exception:
        history = []

    # ── ④ セクター別収束判定 ──────────────────────────────────
    all_results: dict = {}
    alerts:      list = []
    today = datetime.now().strftime("%Y-%m-%d")

    for sector_code, sector_name in jp_sectors.items():
        try:
            res = detect_convergence_for_sector(
                sector_code = str(sector_code),
                sector_name = str(sector_name),
                bundle      = bundle,
                history     = history,
                cfg         = cfg,
            )
            all_results[str(sector_code)] = res

            if res["is_ignition"]:
                alerts.append(res)
                crit_mark = "🚨 最高警戒" if res["is_critical"] else "🔥"
                print(
                    f"    {crit_mark} Ignition検出: {sector_name}（{sector_code}）"
                    f" convergence={res['convergence_score']:.2f}"
                )

        except Exception as _se:
            print(f"    ⚠️  [{sector_code}] 収束判定エラー: {str(_se)[:60]}")

    # ── ⑤ ignition_alerts.json を更新 ────────────────────────
    try:
        alerts_payload = {
            "generated": today,
            "alerts":    alerts,
        }
        _save_ignition_alerts(alerts_payload, cfg)
    except Exception as e:
        print(f"  ⚠️  [ignition_detector] alerts保存エラー: {str(e)[:60]}")

    # ── ⑥ ignition_history.json を更新 ───────────────────────
    try:
        for res in alerts:
            history.append({
                "sector_code":       res["sector_code"],
                "sector_name":       res.get("sector_name", ""),
                "convergence_score": res["convergence_score"],
                "is_critical":       res.get("is_critical", False),
                "detected_at":       today,
            })
        _save_ignition_history(history, cfg)
    except Exception as e:
        print(f"  ⚠️  [ignition_detector] history保存エラー: {str(e)[:60]}")

    # ── ⑦ サマリー生成 ──────────────────────────────────────
    ignition_count = len(alerts)
    critical_count = sum(1 for a in alerts if a.get("is_critical"))
    summary = (
        f"発火{ignition_count}セクター"
        f"（うち最高警戒{critical_count}件）"
        f" | 対象{len(jp_sectors)}業種"
    )

    print(f"  ✅ [ignition_detector] 完了: {summary}")

    return {
        "alerts":         alerts,
        "all_results":    all_results,
        "ignition_count": ignition_count,
        "critical_count": critical_count,
        "summary":        summary,
        "generated":      today,
    }


# ==============================================================
# BLOCK 8: 参照ユーティリティ（alert_notifier / weekly_report 用）
# ==============================================================

def load_current_alerts(cfg=None) -> list:
    """
    ignition_alerts.json から現在のアラートリストを返す。
    alert_notifier.py のトリガー㉑ / weekly_report_generator.py が参照する。

    Returns:
        [{sector_code, sector_name, convergence_score, is_critical, ...}, ...]
    """
    try:
        raw = _load_ignition_alerts(cfg)
        return list(raw.get("alerts", []))
    except Exception:
        return []


def get_sector_convergence_score(
    sector_code: str,
    cfg=None,
) -> float:
    """
    指定セクターの最新 convergence_score を返す。
    alert_notifier.py のトリガー⑳（revision_score との重複チェック）で参照。

    Returns:
        float: convergence_score (0.0 if not found)
    """
    try:
        alerts = load_current_alerts(cfg)
        for a in alerts:
            if str(a.get("sector_code", "")) == str(sector_code):
                return float(a.get("convergence_score", 0.0))
        return 0.0
    except Exception:
        return 0.0


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  ignition_detector.py v1.0 単体テスト")
    print("=" * 60)

    import tempfile

    class _TmpCfg:
        CACHE_DIR                     = Path(tempfile.mkdtemp())
        IGNITION_CONVERGENCE_WINDOW_DAYS = 14
        IGNITION_MIN_SCORE_LINE         = 3.0
        IGNITION_MAX_SCORE_LINE_CRITICAL = 3.5
        IGNITION_CRITICAL_REPEAT_DAYS   = 14
        IGNITION_ALERT_TTL_DAYS         = 14
        IGNITION_HISTORY_TTL_DAYS       = 180
        IGNITION_WEIGHT_CATEGORY_A      = 0.40
        IGNITION_WEIGHT_CATEGORY_B      = 0.35
        IGNITION_WEIGHT_CATEGORY_C      = 0.25
        PEAD_TRIGGER_MIN_REVISION       = 0.3
        JP_SECTORS = {
            "1016": "電気機器",
            "1025": "情報・通信業",
            "1007": "化学",
        }

    _cfg = _TmpCfg()

    # ── モックデータバンドル ──────────────────────────────────
    mock_results = {
        "screener": {
            "passed": [
                {
                    "ticker": "6758.T", "sector_code": "1016",
                    "composite_score": 78.0, "week52_change": 0.22,
                    "risk_adjusted_score": 73.0, "inst_ownership": 0.68,
                    "revision_score": 0.4,
                },
                {
                    "ticker": "6861.T", "sector_code": "1016",
                    "composite_score": 72.0, "week52_change": 0.18,
                    "risk_adjusted_score": 68.0, "inst_ownership": 0.72,
                    "revision_score": 0.3,
                },
            ]
        },
        "signal_aggregator": {
            "1016": {
                "signal_score": 72.0,
                "components": {"money_flow": 75.0, "sector_score": 70.0},
            }
        },
        "sectors": {
            "1016": {
                "macro_signals": [
                    {"signal": "tailwind", "label": "円安恩恵"},
                    {"signal": "tailwind", "label": "AI半導体需要"},
                ]
            }
        },
        "arxiv": {
            "papers": [
                {"sectors": ["1016"], "title": "AI chip performance"},
                {"sectors": ["1016"], "title": "Power semiconductor"},
                {"sectors": ["1016"], "title": "Edge AI efficiency"},
            ]
        },
        "sector_rotation": {
            "sectors": {
                "1016": {"rotation_score": 80.0}
            }
        },
    }

    print("\n── mock_results を使用した収束判定 ──")
    result = run_ignition_detector(_cfg, results=mock_results)
    print(f"\n  ignition_count : {result['ignition_count']}")
    print(f"  critical_count : {result['critical_count']}")
    print(f"  summary        : {result['summary']}")

    if result["alerts"]:
        alert = result["alerts"][0]
        print(f"\n  最初のアラート:")
        print(f"    sector     : {alert['sector_name']}（{alert['sector_code']}）")
        print(f"    conv_score : {alert['convergence_score']:.2f}")
        print(f"    is_crit    : {alert['is_critical']}")
        print(f"    cat_a      : score={alert['category_a']['score']:.3f} "
              f"signals={alert['category_a']['signals']}")
        print(f"    cat_b      : score={alert['category_b']['score']:.3f} "
              f"signals={alert['category_b']['signals']}")
        print(f"    cat_c      : score={alert['category_c']['score']:.3f} "
              f"signals={alert['category_c']['signals']}")

    print("\n── _check_critical_repeat テスト ──")
    mock_history = [
        {"sector_code": "1016", "convergence_score": 3.8,
         "detected_at": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")},
    ]
    is_crit = _check_critical_repeat("1016", mock_history, _cfg)
    print(f"  7日前に3.8の履歴あり → is_critical={is_crit}（True期待）")

    print("\n✅ 単体テスト完了")
