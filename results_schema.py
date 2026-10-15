# ==============================================================
# results_schema.py  v1.0  (Wave PIPE-2)
# results ディクショナリのスキーマ契約
# ==============================================================
# 【役割】
#   run_all_tasks() の results dict は全モジュール間の唯一のデータ受け渡し
#   契約であり、producer/consumer のキー不整合はサイレントに機能が死ぬ。
#   本モジュールはその契約を明文化し、乖離を検出する。
#
# 【SCHEMA の構造】
#   { results_key: [産出モジュール名, ...] }
#   ※ 全コードベースの AST スキャンから自動生成したもの。
#     以後の追加キーは本スキーマに追記すること（--audit が乖離を検出）。
#
# 【使い方】
#   ランタイム検証（main の台帳確定直前に組込済み・警告のみ）:
#     from results_schema import validate_results
#     report = validate_results(results)
#   静的監査（スキーマ vs 現コードの乖離 / CIゲート対応）:
#     python3 results_schema.py --audit
# ==============================================================

from __future__ import annotations

# 自動生成: writer 全数スキャンの結果
SCHEMA: dict[str, list[str]] = {
    "_cfg": ["stock_analyzer_main"],
    "_macro_weights": ["stock_analyzer_main"],
    "_score_history_prev_avg": ["rule_optimizer"],
    "alerts": ["stock_analyzer_main"],
    "alpha_attribution": ["stock_analyzer_main"],
    "anomaly": ["stock_analyzer_main"],
    "anomaly_season": ["run_headless", "stock_analyzer_main"],
    "api_key_health": ["stock_analyzer_main"],
    "arxiv": ["stock_analyzer_main"],
    "backtest": ["stock_analyzer_main"],
    "biz_summaries": ["stock_analyzer_main"],
    "bond_commodity": ["stock_analyzer_main"],
    "catalyst": ["stock_analyzer_main"],
    "ceo_scores": ["stock_analyzer_main"],
    "seisakudb": ["stock_analyzer_main"],
    "public_funding": ["stock_analyzer_main"],
    "cluster_export": ["cluster_graph_exporter"],
    "cluster_master": ["technology_mapper"],
    "cluster_phase": ["stock_analyzer_main", "technology_cluster_engine"],
    "comps_analyzer": ["stock_analyzer_main"],
    "confidence_prior": ["stock_analyzer_main"],
    "congress_monitor": ["stock_analyzer_main"],
    "corr_constraint": ["stock_analyzer_main"],
    "correlation_heatmap": ["stock_analyzer_main"],
    "credit_data": ["stock_analyzer_main"],
    "cross_sector_signals": ["stock_analyzer_main"],
    "cross_size_signal": ["stock_analyzer_main"],
    "data_sla_report": ["stock_analyzer_main"],
    "dcf_valuation": ["stock_analyzer_main"],
    "dd_state": ["stock_analyzer_main"],
    "debt_cycle": ["stock_analyzer_main"],
    "deep_dive": ["stock_analyzer_main"],
    "diversity": ["stock_analyzer_main"],
    "dividend_calendar": ["stock_analyzer_main"],
    "drift_events": ["stock_analyzer_main"],
    "drift_report": ["stock_analyzer_main"],
    "earnings_surprise": ["stock_analyzer_main"],
    "economic_cycle": ["stock_analyzer_main"],
    "edinet_db_fresh": ["cache_freshness_manager"],
    "edinet_doc_index": ["stock_analyzer_main"],
    "edinetdb_ingest": ["stock_analyzer_main"],
    "exit_alerts": ["stock_analyzer_main"],
    "edinet_xbrl_fetch": ["edinet_rotation_manager"],
    "emergency_override": ["stock_analyzer_main"],
    "emerging_tech": ["emerging_technology_detector"],
    "etf_tracker": ["stock_analyzer_main"],
    "event_horizon": ["stock_analyzer_main"],
    "options_advisor": ["stock_analyzer_main"],
    "evaluations": ["stock_analyzer_main"],
    "executive_transition": ["stock_analyzer_main"],
    "facility_expansion": ["stock_analyzer_main"],
    "fudosan_ingest": ["stock_analyzer_main"],
    "factor_crowding": ["stock_analyzer_main"],
    "fed_calendar": ["stock_analyzer_main"],
    "freshness": ["stock_analyzer_main"],
    "geo_risk": ["stock_analyzer_main"],
    "gmail": ["alert_notifier"],
    "google_trends": ["stock_analyzer_main"],
    "gov": ["stock_analyzer_main"],
    "ideas": ["stock_analyzer_main"],
    "ignition": ["stock_analyzer_main"],
    "imf": ["stock_analyzer_main"],
    "independence_scores": ["stock_analyzer_main"],
    "inference": ["stock_analyzer_main"],
    "insider_monitor": ["stock_analyzer_main"],
    "institutional_13f": ["stock_analyzer_main"],
    "institutional_scorer": ["stock_analyzer_main"],
    "job_posting": ["stock_analyzer_main"],
    "jp_us_bridge": ["stock_analyzer_main"],
    "jp_us_sector_correlation": ["stock_analyzer_main"],
    "latent_pearl": ["stock_analyzer_main"],
    "leading_indicator_signals": ["stock_analyzer_main"],
    "learning_audit": ["stock_analyzer_main"],
    "line": ["alert_notifier"],
    "longterm_scenario": ["stock_analyzer_main"],
    "macro": ["stock_analyzer_main"],
    "macro_delta_alerts": ["stock_analyzer_main"],
    "macro_signals": ["stock_analyzer_main"],
    "macro_snapshot": ["stock_analyzer_main"],
    "market_breadth": ["stock_analyzer_main"],
    "midterm_tracker": ["stock_analyzer_main"],
    "ml_correction": ["stock_analyzer_main"],
    "ml_meta_correction": ["stock_analyzer_main"],   # R5-1: ML補正適用結果
    "ml_meta_dataset": ["stock_analyzer_main"],      # R5-1: 特徴量スナップショット結果
    "module_ledger": ["stock_analyzer_main"],
    "momentum": ["stock_analyzer_main"],
    "multi_currency": ["stock_analyzer_main"],
    "news": ["stock_analyzer_main"],
    "notion_obsidian": ["stock_analyzer_main"],
    "ontology_export": ["stock_analyzer_main"],
    "paper_summary": ["stock_analyzer_main"],
    "options_monitor": ["run_headless", "stock_analyzer_main"],
    "pead": ["stock_analyzer_main"],
    "performance": ["weekly_report_generator"],
    "policy": ["stock_analyzer_main"],
    "policy_events_30d": ["stock_analyzer_main"],
    "policy_radar": ["stock_analyzer_main"],
    "nedo_selection": ["stock_analyzer_main"],
    "stock_snapshot": ["stock_analyzer_main"],
    "price_history": ["stock_analyzer_main"],
    "policy_signals": ["stock_analyzer_main"],
    "politician_monitor": ["stock_analyzer_main"],
    "politician_scorer": ["stock_analyzer_main"],
    "portfolio_optimizer": ["stock_analyzer_main"],
    "position_sizer": ["stock_analyzer_main"],
    "regime": ["stock_analyzer_main"],
    "rule_optimizer": ["stock_analyzer_main"],
    "scenario_analysis": ["stock_analyzer_main"],
    "candidate_kpi": ["stock_analyzer_main"],
    "screener": ["institutional_signal_scorer", "stock_analyzer_main"],
    "screener_candidates": ["stock_analyzer_main"],
    "screener_trace": ["stock_analyzer_main"],
    "screener_feedback": ["stock_analyzer_main"],
    "screener_heatmap": ["stock_analyzer_main"],
    "sec_summary": ["stock_analyzer_main"],
    "sector_alloc": ["stock_analyzer_main"],
    "sector_rotation": ["stock_analyzer_main"],
    "sector_tier": ["stock_analyzer_main"],
    "sectors": ["stock_analyzer_main"],
    "self_evaluator": ["stock_analyzer_main"],
    "sentiment": ["stock_analyzer_main"],
    "signal_aggregator": ["stock_analyzer_main"],
    "signal_dedup": ["stock_analyzer_main"],
    "size_decay": ["stock_analyzer_main"],
    "size_tier_master": ["stock_analyzer_main"],
    "stock_detail_report": ["stock_analyzer_main"],
    "strategy_summary": ["stock_analyzer_main"],
    "stress_test": ["stock_analyzer_main"],
    "support_resistance": ["stock_analyzer_main"],
    "tdnet": ["stock_analyzer_main"],
    "technical_scorer": ["stock_analyzer_main"],
    "taxonomy_signal": ["stock_analyzer_main"],
    "technology_mapper": ["stock_analyzer_main"],
    "timeline": ["stock_analyzer_main"],
    "total_signal_score": ["stock_analyzer_main"],
    "unified_scores": ["stock_analyzer_main"],       # R5-2 Phase A: JP/US共通スコア
    "stock_real_estate": ["stock_analyzer_main"],
    "rental_property_fv": ["stock_analyzer_main"],   # 賃貸等不動産の取得結果（rental_property_ingestor.run）    # 全上場の銘柄単位不動産ボーナス（stock_real_estate.run）
    "us_8k": ["stock_analyzer_main"],
    "us_fundamentals": ["stock_analyzer_main"],
    "us_macro": ["stock_analyzer_main"],
    "us_screener": ["stock_analyzer_main"],
    "us_sector_bridge": ["stock_analyzer_main"],
    "us_sectors": ["stock_analyzer_main"],
    "var_constraint": ["stock_analyzer_main"],
    "volume_profile": ["stock_analyzer_main"],
    "wave7_trackers": ["stock_analyzer_main"],
    "weekly_report": ["stock_analyzer_main"],
}

# results に存在してもスキーマ検証対象外とする内部キー
INTERNAL_KEYS = {"_cfg", "_macro_weights", "_schema_check"}


def validate_results(results: dict) -> dict:
    """
    実行後の results をスキーマと突合する（警告のみ・例外を出さない）。

    Returns:
        {"unknown_keys": [...],   # スキーマ未登録キー（契約外の書き込み）
          "checked": int}
    """
    try:
        unknown = sorted(
            k for k in results
            if k not in SCHEMA and k not in INTERNAL_KEYS
        )
        if unknown:
            print(f"  📋 [schema] 契約外の results キー {len(unknown)}件: "
                  f"{', '.join(unknown[:8])}"
                  + (" ..." if len(unknown) > 8 else ""))
            print("     → results_schema.py の SCHEMA に追記してください")
        return {"unknown_keys": unknown, "checked": len(results)}
    except Exception as e:
        return {"unknown_keys": [], "checked": 0, "error": str(e)[:80]}


def audit() -> int:
    """スキーマと現コードの writer 全数を突合する（静的・CIゲート対応）。"""
    import ast
    from pathlib import Path
    repo = Path(__file__).parent
    actual: dict[str, set] = {}
    for f in sorted(repo.glob("*.py")):
        if f.name == "results_schema.py":
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            keys = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "results"
                            and isinstance(t.slice, ast.Constant)):
                        keys.append(t.slice.value)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "results"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.append(node.args[0].value)
            for k in keys:
                if isinstance(k, str):
                    actual.setdefault(k, set()).add(f.stem)

    missing = sorted(set(actual) - set(SCHEMA) - INTERNAL_KEYS)  # コードにあるが契約なし（内部キー除外）
    stale   = sorted(set(SCHEMA) - set(actual))      # 契約にあるがコードなし
    print(f"resultsスキーマ監査: 契約{len(SCHEMA)}キー / 実コード{len(actual)}キー")
    for k in missing:
        print(f"  ❌ 契約未登録: '{k}' （produced by {sorted(actual[k])}）")
    for k in stale:
        print(f"  ⚠️  契約陳腐化: '{k}' （もはやどのコードも書いていない）")
    if not missing and not stale:
        print("  ✅ 契約とコードは完全一致")
    return 1 if missing else 0


if __name__ == "__main__":
    import sys
    sys.exit(audit() if "--audit" in sys.argv else 0)
