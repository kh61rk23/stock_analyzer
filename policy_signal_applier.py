# ==============================================================
# policy_signal_applier.py  v1.0  (Wave 3 - Policy Ph2)
# 政策シグナル 4層キャッシュ減衰・業種適用・緊急フラグ管理
# ==============================================================
# 【役割】
#   politician_signal_scorer.py が生成したセクターブーストと
#   politician_monitor.py のJP波及シグナルを、4層減衰キャッシュ方式で
#   stock_screener 通過銘柄の composite_score に日次適用する。
#
# 【4層キャッシュ減衰設計】
#   シグナルは登録日からの経過日数に応じて4段階に減衰する。
#   $strength = base\_adj \times decay\_factor$
#
#   Layer 1 (day 0-3):   decay=1.00（100%強度）
#   Layer 2 (day 4-7):   decay=0.75（75%）
#   Layer 3 (day 8-14):  decay=0.50（50%）
#   Layer 4 (day 15-30): decay=0.25（25%）
#   day>30:              decay=0.00（TTL切れ・自動削除）
#
# 【EMERGENCY_OVERRIDE_FLAG】
#   VIX > EMERGENCY_VIX_THRESHOLD(45.0) 時に True となり、
#   全政策シグナルの適用を一時停止する（リスクオフ局面の誤信号防止）。
#   VIX 正常化後は自動解除。
#
# 【Level 4 Override 適用】
#   sector_boosts（Level 4 対象セクター）の銘柄に対し、
#   $composite\_score += \min(SCORE\_MAX\_POLICY\_L4\_ADJ, level4\_adj)$
#   を直接加算。通常シグナルより優先して適用。
#
# 【生成キャッシュ】
#   policy_short_cache.json  TTL: 30日
#
# 【呼び出しパターン（stock_analyzer_main.py から日次実行）】
#   from policy_signal_applier import run_policy_signal_applier
#   pol_result = run_policy_signal_applier(results, cfg)
#   results["policy_signals"] = pol_result
#
# 【依存】
#   politician_signal_scorer.py
#   config.py（BLOCK 26/31定数参照）
#   ★ 全関数を try/except で保護。失敗時は元スコアを維持する。
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ==============================================================
# BLOCK 0: 定数
# ==============================================================

# 4層減衰テーブル: (day_upper_bound, decay_factor)
_DECAY_LAYERS: list[tuple[int, float]] = [
    (3,  1.00),   # Layer 1: day 0-3
    (7,  0.75),   # Layer 2: day 4-7
    (14, 0.50),   # Layer 3: day 8-14
    (30, 0.25),   # Layer 4: day 15-30
]
from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B4_POLICY_SIGNAL_APPLIER_DECAY_TTL_DAYS': '_DECAY_TTL_DAYS',
    'B6_POLICY_SIGNAL_APPLIER_DEFAULT_EMERGENCY_VIX': '_DEFAULT_EMERGENCY_VIX',
    'B6_POLICY_SIGNAL_APPLIER_DEFAULT_L4_MAX_ADJ': '_DEFAULT_L4_MAX_ADJ',
}

_DECAY_TTL_DAYS = 30   # この日数を超えたシグナルは削除

# デフォルト定数（config.py BLOCK 31 で上書き可能）
_DEFAULT_EMERGENCY_VIX = 45.0
_DEFAULT_L4_MAX_ADJ    = 25.0


# ==============================================================
# BLOCK 1: キャッシュ I/O
# ==============================================================

def _cache_dir(cfg=None) -> Path:
    try:
        return Path(getattr(cfg, "CACHE_DIR", "/tmp"))
    except Exception:
        return Path("/tmp")


def load_policy_short_cache(cfg=None) -> dict:
    """
    policy_short_cache.json を読み込む。

    Returns:
        {
          "signals": [
            {
              "sector_code":  str,
              "base_adj":     float,    # 登録時点の最大加算量
              "direction":    str,      # "buy" | "sell"
              "registered_at": str,     # "YYYY-MM-DD"
              "source":       str,      # "l4_override" | "jp_signal" | "manual"
              "politician":   str,
              "asset":        str,
            }, ...
          ],
          "emergency_override": bool,
          "emergency_since":    str | None,
          "last_updated":       str,
        }
    """
    try:
        p = _cache_dir(cfg) / "policy_short_cache.json"
        if not p.exists():
            return _empty_cache()

        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _empty_cache()

        # TTL チェック: 登録日が 30日 以上前のシグナルを除去
        cutoff = (datetime.now() - timedelta(days=_DECAY_TTL_DAYS)
                  ).strftime("%Y-%m-%d")
        signals = [
            s for s in raw.get("signals", [])
            if isinstance(s, dict)
            and s.get("registered_at", "0000") >= cutoff
        ]
        raw["signals"] = signals
        return raw

    except Exception:
        return _empty_cache()


def save_policy_short_cache(cache: dict, cfg=None) -> None:
    """policy_short_cache.json を保存する。"""
    try:
        p = _cache_dir(cfg) / "policy_short_cache.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _empty_cache() -> dict:
    return {
        "signals":           [],
        "emergency_override": False,
        "emergency_since":   None,
        "last_updated":      "",
    }


# ==============================================================
# BLOCK 2: 4層減衰計算
# ==============================================================

def get_decay_factor(registered_at: str) -> float:
    """
    登録日からの経過日数に対応する減衰係数を返す。

    Args:
        registered_at: "YYYY-MM-DD" 形式の登録日

    Returns:
        float: 0.00〜1.00（TTL切れは0.00）
    """
    try:
        reg_dt   = datetime.strptime(registered_at[:10], "%Y-%m-%d")
        days_ago = (datetime.now() - reg_dt).days

        for upper, factor in _DECAY_LAYERS:
            if days_ago <= upper:
                return factor
        return 0.0   # TTL切れ

    except Exception:
        return 0.0


def calc_effective_adj(signal: dict) -> float:
    """
    1シグナルエントリの実効加算量（decay適用後）を返す。

    $effective\\_adj = base\\_adj \times decay\\_factor$
    sell 方向は負値で返す。

    Returns:
        float: 実効加算量
    """
    try:
        base      = float(signal.get("base_adj", 0.0))
        direction = str(signal.get("direction", "buy"))
        registered = str(signal.get("registered_at", ""))
        decay     = get_decay_factor(registered)

        adj = base * decay
        if direction == "sell":
            adj = -adj

        return round(adj, 2)
    except Exception:
        return 0.0


def get_sector_effective_boosts(
    cache: dict,
    cfg=None,
) -> dict[str, float]:
    """
    キャッシュ内の全シグナルから、セクター別の実効ブースト量を集計する。

    同一セクターに複数のシグナルがある場合は加算。
    クランプ: $[-SCORE\\_MAX\\_L4\\_ADJ, +SCORE\\_MAX\\_L4\\_ADJ]$

    Returns:
        {sector_code: effective_boost_pt, ...}
    """
    try:
        max_adj = float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ", _DEFAULT_L4_MAX_ADJ))
        boosts:  dict[str, float] = {}

        for signal in cache.get("signals", []):
            sc  = str(signal.get("sector_code", ""))
            adj = calc_effective_adj(signal)
            if sc and adj != 0.0:
                current = boosts.get(sc, 0.0)
                boosts[sc] = round(
                    max(-max_adj, min(max_adj, current + adj)), 2
                )
        return boosts
    except Exception:
        return {}


# ==============================================================
# BLOCK 3: EMERGENCY_OVERRIDE_FLAG 管理
# ==============================================================

def check_emergency_override(
    cache:   dict,
    results: Optional[dict] = None,
    cfg=None,
) -> tuple[bool, dict]:
    """
    VIX レベルを確認し EMERGENCY_OVERRIDE_FLAG を更新する。

    VIX 値は results["macro"] または results["signal_aggregator"] から取得。
    取得不可の場合は既存フラグを維持。

    Args:
        cache:   load_policy_short_cache() の返り値
        results: stock_analyzer_main.results 辞書
        cfg:     config モジュール（None 可）

    Returns:
        (is_emergency: bool, updated_cache: dict)
    """
    try:
        vix_threshold = float(
            getattr(cfg, "EMERGENCY_VIX_THRESHOLD", _DEFAULT_EMERGENCY_VIX)
        )
        today = datetime.now().strftime("%Y-%m-%d")

        # VIX 値を複数ソースから取得
        current_vix: Optional[float] = None
        if results:
            # macro シグナルから VIX を探す
            for key in ("macro", "macro_signals", "vix_data"):
                macro = results.get(key)
                if isinstance(macro, dict):
                    vix_val = macro.get("vix") or macro.get("VIX")
                    if vix_val is not None:
                        current_vix = float(vix_val)
                        break
                elif isinstance(macro, list):
                    for item in macro:
                        if isinstance(item, dict):
                            vix_val = item.get("vix") or item.get("VIX")
                            if vix_val is not None:
                                current_vix = float(vix_val)
                                break

        # VIX データなし → 既存フラグを維持
        if current_vix is None:
            return bool(cache.get("emergency_override", False)), cache

        # フラグ更新
        is_emergency = current_vix > vix_threshold
        was_emergency = bool(cache.get("emergency_override", False))

        if is_emergency and not was_emergency:
            cache["emergency_override"] = True
            cache["emergency_since"]    = today
            print(f"  🚨 EMERGENCY_OVERRIDE_FLAG ON: VIX={current_vix:.1f}"
                  f" > {vix_threshold:.1f}")
        elif not is_emergency and was_emergency:
            cache["emergency_override"] = False
            cache["emergency_since"]    = None
            print(f"  ✅ EMERGENCY_OVERRIDE_FLAG OFF: VIX正常化({current_vix:.1f})")

        return is_emergency, cache

    except Exception:
        return bool(cache.get("emergency_override", False)), cache


def get_emergency_override_flag(
    results: Optional[dict] = None,
    cfg=None,
) -> bool:
    """
    現在の EMERGENCY_OVERRIDE_FLAG の状態を返す。
    stock_analyzer_main.py やスクリーナーから参照する簡易ラッパー。
    """
    try:
        cache = load_policy_short_cache(cfg)
        is_em, _ = check_emergency_override(cache, results, cfg)
        return is_em
    except Exception:
        return False


# ==============================================================
# BLOCK 4: シグナルをキャッシュに登録
# ==============================================================

def register_signals(
    cache:          dict,
    sector_boosts:  dict[str, float],
    source:         str  = "jp_signal",
    politician:     str  = "",
    asset:          str  = "",
    direction:      str  = "buy",
    cfg=None,
) -> dict:
    """
    セクター別ブーストをキャッシュに登録する。

    既存の同一 (sector_code, source) エントリがある場合は上書き更新する。

    Args:
        cache:          load_policy_short_cache() の返り値
        sector_boosts:  {sector_code: adj_pt}
        source:         "l4_override" | "jp_signal" | "manual"
                        | "institutional_13f" | "policy_radar"
        politician:     議員名（トレーサビリティ用）
        asset:          銘柄/資産名（トレーサビリティ用）
        direction:      "buy" | "sell"
        cfg:            config モジュール（None 可）

    Returns:
        更新済み cache dict
    """
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        signals  = list(cache.get("signals", []))
        max_adj  = float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ", _DEFAULT_L4_MAX_ADJ))

        for sc, adj in sector_boosts.items():
            if abs(adj) < 0.01:
                continue   # 微小シグナルは登録しない

            # 既存エントリを更新（同セクター・同ソース）
            updated = False
            for sig in signals:
                if sig.get("sector_code") == sc and sig.get("source") == source:
                    sig["base_adj"]      = round(min(max_adj, abs(adj)), 2)
                    sig["direction"]     = direction if adj >= 0 else "sell"
                    sig["registered_at"] = today
                    sig["politician"]    = politician
                    sig["asset"]         = asset
                    updated = True
                    break

            if not updated:
                signals.append({
                    "sector_code":   sc,
                    "base_adj":      round(min(max_adj, abs(adj)), 2),
                    "direction":     direction if adj >= 0 else "sell",
                    "registered_at": today,
                    "source":        source,
                    "politician":    politician,
                    "asset":         asset,
                })

        cache["signals"]      = signals
        cache["last_updated"] = today
        return cache

    except Exception:
        return cache


# ==============================================================
# BLOCK 5: screener候補への適用
# ==============================================================

def apply_policy_signals_to_screener(
    passed:         list[dict],
    effective_boosts: dict[str, float],
    is_emergency:   bool,
    l4_sector_boosts: dict[str, float],
    cfg=None,
) -> list[dict]:
    """
    screener 通過銘柄リストに政策シグナルブーストを適用する。

    適用優先順:
      1. Level 4 Override（l4_sector_boosts）を先に適用
      2. 通常シグナル（effective_boosts）を加算
    EMERGENCY フラグが True の場合は全適用をスキップ。

    Args:
        passed:             screener 通過銘柄リスト
        effective_boosts:   get_sector_effective_boosts() の返り値
        is_emergency:       EMERGENCY_OVERRIDE_FLAG の状態
        l4_sector_boosts:   build_l4_sector_boosts() の返り値（Level 4専用）
        cfg:                config モジュール（None 可）

    Returns:
        政策シグナルが適用された銘柄リスト（composite_score 更新済み）
    """
    try:
        if is_emergency:
            print("  🚨 EMERGENCY_OVERRIDE_FLAG=True: 政策シグナル適用スキップ")
            return passed

        max_adj = float(getattr(cfg, "SCORE_MAX_POLICY_L4_ADJ", _DEFAULT_L4_MAX_ADJ))
        applied_count = 0

        for c in passed:
            sc = str(c.get("sector_code", ""))
            if not sc:
                continue

            total_adj = 0.0

            # ── Level 4 Override を先に適用 ─────────────────────
            l4_adj = float(l4_sector_boosts.get(sc, 0.0))
            if l4_adj != 0.0:
                total_adj += l4_adj
                c["policy_l4_adj"]    = l4_adj
                c["policy_l4_active"] = True

            # ── 通常シグナルを加算 ───────────────────────────────
            std_adj = float(effective_boosts.get(sc, 0.0))
            if std_adj != 0.0:
                total_adj += std_adj
                c["policy_signal_adj"] = std_adj

            if total_adj == 0.0:
                continue

            # composite_score に加算（上限 100pt・下限 0pt）
            original = float(c.get("composite_score", 0.0))
            clamped  = round(
                max(0.0, min(100.0, original + total_adj)), 1
            )
            c["composite_score"]        = clamped
            c["policy_total_adj"]       = round(total_adj, 2)
            c["policy_signal_applied"]  = True
            applied_count += 1

        if applied_count:
            print(f"  📋 政策シグナル適用: {applied_count}銘柄のcomposite_scoreを更新")

        return passed

    except Exception:
        return passed


# ==============================================================
# BLOCK 6: メインエントリポイント（日次実行）
# ==============================================================

def run_policy_signal_applier(
    results: Optional[dict] = None,
    cfg=None,
) -> dict:
    """
    Policy Signal Applier の日次メインエントリポイント。
    stock_analyzer_main.py のフラグチェック後に呼ばれる。

    処理フロー:
      1. policy_short_cache.json を読み込む
      2. EMERGENCY_OVERRIDE_FLAG を確認・更新
      3. politician_signal_scorer の結果からシグナルを登録
      4. politician_monitor の JP 波及シグナルを登録
      5. [Wave 4 Ph3] institutional_13f シグナルを登録
      6. [Wave 4 Ph3] policy_medium_cache (L3) からシグナルを統合
      7. 減衰適用後の有効ブーストを計算
      8. screener 通過銘柄に適用
      9. キャッシュを保存して結果を返す

    Args:
        results: stock_analyzer_main.results 辞書（None 可）
        cfg:     config モジュール（None 可）

    Returns:
        {
          "effective_boosts":    dict,   # セクター別有効ブースト量
          "l4_sector_boosts":    dict,   # Level 4 セクター別ブースト
          "is_emergency":        bool,
          "signals_registered":  int,
          "applied_count":       int,    # composite_score が更新された銘柄数
          "cache_signal_count":  int,
          "summary":             str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("  📋 [policy_signal_applier] 日次政策シグナル適用中...")

    if results is None:
        results = {}

    # ── ① キャッシュ読み込み ──────────────────────────────────
    try:
        cache = load_policy_short_cache(cfg)
    except Exception:
        cache = _empty_cache()

    # ── ② EMERGENCY_OVERRIDE_FLAG 確認 ───────────────────────
    try:
        is_emergency, cache = check_emergency_override(cache, results, cfg)
    except Exception:
        is_emergency = False

    signals_registered = 0

    # ── ③ politician_signal_scorer の L4 シグナルを登録 ───────
    scorer_result = results.get("politician_scorer", {})
    l4_boosts     = scorer_result.get("sector_boosts", {})

    if l4_boosts and not is_emergency:
        try:
            # L4 Override シグナルは "l4_override" ソースで登録
            cache = register_signals(
                cache         = cache,
                sector_boosts = l4_boosts,
                source        = "l4_override",
                politician    = "L4_Override_Batch",
                asset         = "",
                direction     = "buy",
                cfg           = cfg,
            )
            signals_registered += len(l4_boosts)
            print(f"    ⭐ L4 Overrideシグナル登録: {len(l4_boosts)}業種")
        except Exception as e:
            print(f"    ⚠️  L4 Override登録エラー: {str(e)[:60]}")

    # ── ④ politician_monitor の JP 波及シグナルを登録 ─────────
    # ★ 2026-06-10 バグ修正: main の書き込みキーは "politician_monitor"
    pol_result  = results.get("politician_monitor", results.get("politician", {}))
    jp_signals  = pol_result.get("jp_sector_signals", {})

    if jp_signals and not is_emergency:
        try:
            cache = register_signals(
                cache         = cache,
                sector_boosts = jp_signals,
                source        = "jp_signal",
                politician    = "JP_Wave_Batch",
                asset         = "",
                direction     = "buy",
                cfg           = cfg,
            )
            signals_registered += len(jp_signals)
            print(f"    🇯🇵 JP波及シグナル登録: {len(jp_signals)}業種")
        except Exception as e:
            print(f"    ⚠️  JP波及シグナル登録エラー: {str(e)[:60]}")

    # ── ⑤ [Wave 4 Ph3] institutional_13f シグナルを登録 ────────
    # institutional_signal_scorer が policy_short_cache に登録した
    # institutional_13f ソースのシグナルを反映する。
    # ※ institutional_signal_scorer.py 側で既に登録済みのため、
    #   ここでは results["institutional_scorer"] の sector_boosts を
    #   キャッシュに追記する（二重登録は register_signals の上書き処理で吸収）。
    inst_scorer = results.get("institutional_scorer", {})
    inst_boosts = inst_scorer.get("sector_boosts", {})

    if inst_boosts and not is_emergency:
        try:
            cache = register_signals(
                cache         = cache,
                sector_boosts = inst_boosts,
                source        = "institutional_13f",
                politician    = "institutional_signal_scorer",
                asset         = "",
                direction     = "buy",
                cfg           = cfg,
            )
            signals_registered += len(inst_boosts)
            print(f"    🏦 institutional_13f シグナル登録: "
                  f"{len(inst_boosts)}業種")
        except Exception as e:
            print(f"    ⚠️  institutional_13f 登録エラー: {str(e)[:60]}")

    # ── ⑥ [Wave 4 Ph3] policy_medium_cache (L3) からシグナルを統合 ──
    # policy_medium_cache.json に登録されたシグナルを
    # policy_short_cache の有効ブーストに統合する。
    # L3 シグナルは decay 計算の対象外（有効期間中は固定値）。
    try:
        _medium_path = _cache_dir(cfg) / "policy_medium_cache.json"
        if _medium_path.exists():
            _medium = json.loads(_medium_path.read_text(encoding="utf-8"))
            _medium_sigs = _medium.get("signals", [])
            _medium_boosts: dict = {}
            _today = datetime.now().strftime("%Y-%m-%d")
            _ttl_days = int(getattr(cfg, "POLICY_MEDIUM_CACHE_TTL_DAYS", 90))

            for ms in _medium_sigs:
                sc  = ms.get("sector_code", "")
                adj = float(ms.get("base_adj", 0.0))
                reg = ms.get("registered_at", "")
                if not sc or adj == 0:
                    continue
                # TTL チェック
                try:
                    age = (datetime.now() -
                           datetime.strptime(reg, "%Y-%m-%d")).days
                    if age > _ttl_days:
                        continue
                except Exception:
                    pass
                dir_ = ms.get("direction", "tailwind")
                if dir_ == "headwind":
                    adj = -adj
                _medium_boosts[sc] = round(
                    _medium_boosts.get(sc, 0.0) + adj, 2
                )

            if _medium_boosts and not is_emergency:
                cache = register_signals(
                    cache         = cache,
                    sector_boosts = _medium_boosts,
                    source        = "policy_medium_l3",
                    politician    = "medium_cache_auto",
                    asset         = "",
                    direction     = "buy",
                    cfg           = cfg,
                )
                signals_registered += len(_medium_boosts)
                print(f"    📋 policy_medium_cache (L3) 統合: "
                      f"{len(_medium_boosts)}業種")
    except Exception as e:
        print(f"    ⚠️  medium_cache 統合エラー: {str(e)[:60]}")

    # ── ⑦ 減衰適用後の有効ブーストを計算 ─────────────────────
    try:
        effective_boosts = get_sector_effective_boosts(cache, cfg)
    except Exception:
        effective_boosts = {}

    # ── ⑥ screener 通過銘柄に適用 ────────────────────────────
    applied_count = 0
    try:
        screener  = results.get("screener", {})
        passed    = (screener.get("passed")
                     or screener.get("candidates", []))

        if passed and (effective_boosts or l4_boosts):
            updated = apply_policy_signals_to_screener(
                passed            = passed,
                effective_boosts  = effective_boosts,
                is_emergency      = is_emergency,
                l4_sector_boosts  = l4_boosts,
                cfg               = cfg,
            )
            applied_count = sum(
                1 for c in updated if c.get("policy_signal_applied")
            )
            # results 内の passed を更新
            if "screener" in results:
                if "passed" in results["screener"]:
                    results["screener"]["passed"] = updated
                else:
                    results["screener"]["candidates"] = updated
    except Exception as e:
        print(f"    ⚠️  screener適用エラー: {str(e)[:60]}")

    # ── [Wave 4 Ph4] congress_monitor シグナルを L4 として統合 ────
    # congress_monitor の sector_boosts を
    # policy_structural_cache.json (L4) に登録し、
    # screener 通過銘柄に直接加算する。
    congress_result = results.get("congress_monitor", {})
    congress_boosts = congress_result.get("sector_boosts", {})

    if congress_boosts and not is_emergency:
        try:
            # policy_structural_cache.json (L4) に保存
            _structural_path = _cache_dir(cfg) / "policy_structural_cache.json"
            try:
                if _structural_path.exists():
                    _structural = json.loads(
                        _structural_path.read_text(encoding="utf-8")
                    )
                else:
                    _structural = {"signals": [], "last_updated": ""}
            except Exception:
                _structural = {"signals": [], "last_updated": ""}

            _today = datetime.now().strftime("%Y-%m-%d")
            _ttl_months = int(getattr(cfg,
                "POLICY_STRUCTURAL_CACHE_TTL_MONTHS", 12))

            # 既存の congress エントリを更新（同日分は上書き）
            _existing = {
                s.get("sector_code"): i
                for i, s in enumerate(_structural.get("signals", []))
                if s.get("source") == "congress_monitor"
            }
            for sc, adj in congress_boosts.items():
                entry = {
                    "sector_code":   sc,
                    "base_adj":      round(adj, 2),
                    "direction":     "tailwind" if adj >= 0 else "headwind",
                    "registered_at": _today,
                    "source":        "congress_monitor",
                    "label":         f"議会法案L4 ({sc})",
                    "ttl_months":    _ttl_months,
                }
                if sc in _existing:
                    _structural["signals"][_existing[sc]] = entry
                else:
                    _structural.setdefault("signals", []).append(entry)

            _structural["last_updated"] = _today
            _structural_path.parent.mkdir(parents=True, exist_ok=True)
            _structural_path.write_text(
                json.dumps(_structural, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # screener 通過銘柄に L4 として直接加算
            _l4_max = float(getattr(cfg,
                "SCORE_MAX_POLICY_L4_ADJ_W4", 25.0))
            _screener_upd = results.get("screener", {})
            _passed_upd   = (
                _screener_upd.get("passed")
                or _screener_upd.get("candidates", [])
            )
            _congress_applied = 0
            for _cand in _passed_upd:
                _sc = str(_cand.get("sector_code", ""))
                _cadj = float(congress_boosts.get(_sc, 0.0))
                if _cadj == 0.0:
                    continue
                _cadj_clamped = round(
                    max(-_l4_max, min(_l4_max, _cadj)), 2
                )
                _orig = float(_cand.get("composite_score", 0.0))
                _cand["composite_score"] = round(
                    max(0.0, min(100.0, _orig + _cadj_clamped)), 1
                )
                _cand["congress_l4_adj"] = _cadj_clamped
                _congress_applied += 1

            signals_registered += len(congress_boosts)
            print(
                f"    🏛️  congress L4 シグナル登録: "
                f"{len(congress_boosts)}業種 / "
                f"{_congress_applied}銘柄適用"
            )
        except Exception as e:
            print(f"    ⚠️  congress L4 統合エラー: {str(e)[:60]}")

    # ── [Wave 4 Ph4] L5 緊急オーバーライド ──────────────────────
    # 日次下落率が EMERGENCY_DAILY_DROP_THRESHOLD(-4%) を超えた業種に
    # 緊急フラグを立て、composite_score を強制的にクランプする。
    # 通常の EMERGENCY_OVERRIDE（VIX基準）とは独立して動作する。
    _drop_threshold = float(getattr(cfg,
        "EMERGENCY_DAILY_DROP_THRESHOLD", -0.04))
    _l5_sectors: list[str] = []

    try:
        sector_results = results.get("sectors", {})
        for _sc, _sres in sector_results.items():
            if not isinstance(_sres, dict):
                continue
            # 日次下落率: price_change_pct または daily_return から取得
            _daily_ret = (
                _sres.get("price_change_pct")
                or _sres.get("daily_return")
                or _sres.get("price_change")
            )
            if _daily_ret is None:
                continue
            try:
                _ret = float(_daily_ret)
            except (TypeError, ValueError):
                continue

            if _ret <= _drop_threshold:
                _l5_sectors.append(_sc)

        if _l5_sectors:
            print(
                f"    🚨 [L5 緊急OR] 日次下落{abs(_drop_threshold)*100:.0f}%超: "
                f"{len(_l5_sectors)}業種 → composite_score上限50ptに制限"
            )
            # screener 通過銘柄の該当業種スコアを 50pt に上限制限
            _screener_l5 = results.get("screener", {})
            _passed_l5   = (
                _screener_l5.get("passed")
                or _screener_l5.get("candidates", [])
            )
            for _cand in _passed_l5:
                _csc = str(_cand.get("sector_code", ""))
                if _csc not in _l5_sectors:
                    continue
                _orig_l5 = float(_cand.get("composite_score", 50.0))
                if _orig_l5 > 50.0:
                    _cand["composite_score"] = 50.0
                    _cand["l5_emergency_clamp"] = True
                    _cand["l5_pre_clamp_score"] = round(_orig_l5, 1)
    except Exception as e:
        print(f"    ⚠️  L5 緊急OR エラー: {str(e)[:60]}")

    # L5 発動情報を結果に追記
    l4_boosts = {**l4_boosts, **congress_boosts}   # congress_boosts を l4_boosts にマージ

    # ── ⑦ キャッシュを保存 ───────────────────────────────────
    try:
        save_policy_short_cache(cache, cfg)
    except Exception as e:
        print(f"    ⚠️  キャッシュ保存エラー: {str(e)[:60]}")

    # ── ⑧ サマリー ───────────────────────────────────────────
    cache_signal_count = len(cache.get("signals", []))
    emergency_str      = "🚨EMERGENCY" if is_emergency else "正常"

    # policy_medium_cache のシグナル件数を取得（サマリー表示用）
    _medium_count = 0
    try:
        _mp = _cache_dir(cfg) / "policy_medium_cache.json"
        if _mp.exists():
            _md = json.loads(_mp.read_text(encoding="utf-8"))
            _medium_count = len(_md.get("signals", []))
    except Exception:
        pass

    summary = (
        f"登録シグナル{cache_signal_count}件"
        f"（今回+{signals_registered}件） "
        f"有効ブースト{len(effective_boosts)}業種 "
        f"適用{applied_count}銘柄 "
        f"L3medium={_medium_count}件 "
        f"緊急フラグ:{emergency_str}"
    )

    print(f"  ✅ 政策シグナル適用完了: {summary}")

    return {
        "effective_boosts":        effective_boosts,
        "l4_sector_boosts":        l4_boosts,
        "is_emergency":            is_emergency,
        "signals_registered":      signals_registered,
        "applied_count":           applied_count,
        "cache_signal_count":      cache_signal_count,
        "l5_emergency_sectors":    _l5_sectors,       # Wave 4 Ph4
        "structural_cache_updated": bool(congress_boosts),  # Wave 4 Ph4
        "summary":                 summary,
    }


# ==============================================================
# 単体テスト
# ==============================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("  policy_signal_applier.py v1.0 単体テスト")
    print("=" * 60)

    class _TmpCfg:
        CACHE_DIR                = Path(tempfile.mkdtemp())
        EMERGENCY_VIX_THRESHOLD  = 45.0
        SCORE_MAX_POLICY_L4_ADJ  = 25.0
        POLITICIAN_CACHE_LAYERS  = 4

    _cfg = _TmpCfg()

    # ── 減衰テスト ────────────────────────────────────────────
    print("\n── 4層減衰テスト ──")
    for days_ago, expected_layer in [(0, "L1"), (4, "L2"), (8, "L3"), (15, "L4"), (31, "TTL切れ")]:
        reg_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        factor   = get_decay_factor(reg_date)
        print(f"  day {days_ago:2d}前 ({expected_layer:5s}): decay={factor:.2f}")

    # ── register_signals / get_sector_effective_boosts テスト ──
    print("\n── シグナル登録・ブースト計算テスト ──")
    cache = _empty_cache()
    cache = register_signals(
        cache         = cache,
        sector_boosts = {"1016": 12.0, "1028": 8.0, "1025": 5.0},
        source        = "l4_override",
        politician    = "Test Senator",
        direction     = "buy",
        cfg           = _cfg,
    )
    # 4日前のシグナルも追加（L2 減衰）
    old_sig = {
        "sector_code":   "1007",
        "base_adj":      10.0,
        "direction":     "buy",
        "registered_at": (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
        "source":        "jp_signal",
        "politician":    "Old Signal",
        "asset":         "",
    }
    cache["signals"].append(old_sig)

    boosts = get_sector_effective_boosts(cache, _cfg)
    print(f"  セクター別有効ブースト:")
    for sc, adj in sorted(boosts.items()):
        print(f"    [{sc}]: {adj:+.2f}pt")

    # ── EMERGENCY FLAG テスト ─────────────────────────────────
    print("\n── EMERGENCY_OVERRIDE_FLAG テスト ──")
    mock_results_em   = {"macro": {"vix": 50.0}}
    mock_results_norm = {"macro": {"vix": 20.0}}
    cache_em = _empty_cache()

    is_em, cache_em = check_emergency_override(cache_em, mock_results_em, _cfg)
    print(f"  VIX=50.0 → is_emergency={is_em} (True期待)")
    is_em2, cache_em = check_emergency_override(cache_em, mock_results_norm, _cfg)
    print(f"  VIX=20.0 → is_emergency={is_em2} (False期待)")

    # ── apply_policy_signals_to_screener テスト ──────────────
    print("\n── screener適用テスト ──")
    mock_passed = [
        {"ticker": "6758.T", "sector_code": "1016", "composite_score": 70.0},
        {"ticker": "8306.T", "sector_code": "1028", "composite_score": 65.0},
        {"ticker": "9432.T", "sector_code": "1025", "composite_score": 60.0},
        {"ticker": "4063.T", "sector_code": "1007", "composite_score": 55.0},
    ]
    l4_boosts = {"1016": 15.0}
    applied   = apply_policy_signals_to_screener(
        passed            = mock_passed,
        effective_boosts  = boosts,
        is_emergency      = False,
        l4_sector_boosts  = l4_boosts,
        cfg               = _cfg,
    )
    print(f"  適用後 composite_score:")
    for c in applied:
        orig = {"6758.T": 70.0, "8306.T": 65.0, "9432.T": 60.0, "4063.T": 55.0}
        adj  = c.get("policy_total_adj", 0.0)
        print(f"  [{c['ticker']}] {orig[c['ticker']]:.0f} + {adj:+.1f}"
              f" → {c['composite_score']:.0f}pt"
              f" (L4={c.get('policy_l4_active', False)})")

    # ── run_policy_signal_applier 統合テスト ──────────────────
    print("\n── run_policy_signal_applier 統合テスト ──")
    mock_results = {
        "politician":       {"jp_sector_signals": {"1015": 3.0, "1016": 5.0}},
        "politician_scorer": {"sector_boosts": {"1016": 18.0, "1017": 10.0}},
        "screener":         {"passed": [
            {"ticker": "6301.T", "sector_code": "1015", "composite_score": 72.0},
            {"ticker": "6758.T", "sector_code": "1016", "composite_score": 68.0},
            {"ticker": "7203.T", "sector_code": "1017", "composite_score": 65.0},
        ]},
        "macro": {"vix": 18.5},
    }
    result = run_policy_signal_applier(mock_results, _cfg)
    print(f"  summary: {result['summary']}")
    print(f"  passed 後 composite_score:")
    for c in mock_results["screener"]["passed"]:
        print(f"    [{c['ticker']}] → {c['composite_score']:.1f}pt"
              f" adj={c.get('policy_total_adj', 0):+.1f}")

    print("\n✅ 単体テスト完了")
