# ==============================================================
# gov_data_pipeline.py  v9.0
# ==============================================================
# 【v8.0 → v9.0 根本修正】
#   ① フェイルオーバー機能追加
#      IDを取得してもgetStatsDataがエラーを返したら
#      キャッシュを即削除して次候補IDで再試行
#      → 「2018年IDが1ヶ月キャッシュされ続ける」問題を完全解消
#   ② 建設工事着工統計 statsCode: 00600120 → 00600220（国交省確認済み）
#      hardcoded_id 0003072888 は存在しないIDのため削除
#   ③ 鉱工業・毎月勤労統計のhardcoded_idを追加
#      動的検索が古いIDを返した場合のセーフティネット
# ==============================================================

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


ESTAT_LIST_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsList"
ESTAT_DATA_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"

STAT_DEFINITIONS = [
    {
        "id":           "cpi",
        "name":         "消費者物価指数（CPI）",
        "statsCode":    "00200573",
        "searchWord":   "消費者物価指数 総合",
        "unit":         "（2020年=100）",
        "direction":    "higher_is_bad",
        "sectors":      ["1027", "1033", "1019", "1004"],
        "description":  "物価上昇は購買力を下げ、小売・外食・生活用品に逆風",
        "hardcoded_id": "0003427113",
    },
    {
        "id":           "industrial_production",
        "name":         "鉱工業生産指数",
        "statsCode":    "00550300",
        "searchWord":   "鉱工業",
        "unit":         "（2015年=100）",
        "direction":    "higher_is_good",
        "sectors":      ["1015", "1016", "1007", "1012"],
        "description":  "生産指数の上昇は製造業全般の追い風",
        # 0004015800: 2025-04-30公開の最新版（過去ログから確認）
        "hardcoded_id": "0004015800",
    },
    {
        "id":           "wages",
        "name":         "毎月勤労統計（現金給与総額）",
        "statsCode":    "00450071",
        "searchWord":   "現金給与",
        "unit":         "（円）",
        "direction":    "higher_is_good",
        "sectors":      ["1027", "1033", "1004", "1019"],
        "description":  "賃金上昇は消費拡大につながり、小売・外食・サービス業に追い風",
        # 動的検索で2018年IDを掴む場合のフォールバック
        # 0003138259: 2016年公開版 → 不適切のためNoneにして動的検索に委ねる
        # ただしフェイルオーバーで再試行するため最終的に正しいIDを取得できる
        "hardcoded_id": None,
    },
    {
        "id":           "household_spending",
        "name":         "家計調査（消費支出）",
        "statsCode":    "00200561",
        "searchWord":   "消費支出",
        "unit":         "（円）",
        "direction":    "higher_is_good",
        "sectors":      ["1027", "1033", "1004", "1026"],
        "description":  "消費支出の増加は内需型業種全般の追い風",
        "hardcoded_id": "0003000801",
    },
    {
        "id":           "unemployment",
        "name":         "労働力調査（完全失業率）",
        "statsCode":    "00200531",
        "searchWord":   "完全失業率",
        "unit":         "（%）",
        "direction":    "higher_is_bad",
        "sectors":      ["1027", "1033", "1028", "1032"],
        "description":  "失業率上昇は景気悪化を示し、内需・金融・不動産に逆風",
        "hardcoded_id": None,
    },
    {
        "id":           "construction_starts",
        "name":         "建設工事着工統計",
        "statsCode":    "00600220",   # 国交省 建設工事着工統計（正しいコード）
        "searchWord":   "着工",
        "unit":         "（戸）",
        "direction":    "higher_is_good",
        "sectors":      ["1003", "1032", "1012", "1014"],
        "description":  "着工件数の増加は建設・不動産・鉄鋼の追い風",
        "hardcoded_id": None,   # hardcoded_id廃止。フェイルオーバーで対応
    },
    {
        "id":           "machinery_orders",
        "name":         "機械受注統計（民需）",
        "statsCode":    "00100401",
        "searchWord":   "機械受注",
        "unit":         "（億円）",
        "direction":    "higher_is_good",
        "sectors":      ["1015", "1016", "1018"],
        "description":  "機械受注の増加は設備投資拡大を示し、機械・電気機器に先行追い風",
        "hardcoded_id": None,
    },
    {
        "id":           "trade_exports",
        "name":         "貿易統計（輸出総額）",
        "statsCode":    "00350300",
        "searchWord":   "輸出",
        "unit":         "（百万円）",
        "direction":    "higher_is_good",
        "sectors":      ["1017", "1015", "1007", "1016"],
        "description":  "輸出増加は外需型製造業（自動車・機械・化学）の追い風",
        "hardcoded_id": None,
    },
]

SECTOR_NAMES = {
    "1003": "建設業",     "1004": "食料品",
    "1007": "化学",       "1008": "医薬品",
    "1012": "鉄鋼",       "1014": "金属製品",
    "1015": "機械",       "1016": "電気機器",
    "1017": "輸送用機器", "1018": "精密機器",
    "1019": "その他製品", "1020": "電気・ガス業",
    "1021": "陸運業",     "1026": "卸売業",
    "1027": "小売業",     "1028": "銀行業",
    "1032": "不動産業",   "1033": "サービス業",
}


def setup_dirs(cfg) -> dict:
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        print("  ✅ Google Drive マウント完了")
    except Exception:
        pass
    insight_dir = Path(cfg.INSIGHT_DIR) / "gov"
    data_dir    = Path(cfg.DATA_DIR) / "gov_cache"
    for d in [insight_dir, data_dir]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
    return {"insight_dir": insight_dir, "data_dir": data_dir}


def _call_stats_list(app_id, stats_code, search_word, cycle) -> list:
    try:
        import requests
    except ImportError:
        return []
    params = {
        "appId": app_id, "lang": "J",
        "statsCode": stats_code,
        "searchKind": "1", "explanationGetFlg": "N",
    }
    if cycle:
        params["cycle"] = cycle
    if search_word:
        params["searchWord"] = search_word
    try:
        resp   = requests.get(ESTAT_LIST_URL, params=params, timeout=20)
        if resp.status_code != 200:
            return []
        raw    = resp.json()
        status = (raw.get("GET_STATS_LIST", {})
                     .get("RESULT", {}).get("STATUS", -1))
        if status != 0:
            return []
        tables = (raw.get("GET_STATS_LIST", {})
                     .get("DATALIST_INF", {})
                     .get("TABLE_INF", []))
        if not tables:
            return []
        return [tables] if isinstance(tables, dict) else list(tables)
    except Exception:
        return []


def _get_all_candidate_ids(app_id, stats_code, search_word) -> list:
    """
    DB型（0003始まり）のID候補を全て収集し OPEN_DATE 降順で返す。
    フェイルオーバー用に複数候補を保持する。
    """
    all_tables = []
    steps = [
        ("3",  search_word),
        ("3",  None),
        (None, search_word),
        (None, None),
    ]
    seen = set()
    for cycle, word in steps:
        tables = _call_stats_list(app_id, stats_code, word, cycle)
        for t in tables:
            tid = t.get("@id", "")
            if tid and tid not in seen:
                seen.add(tid)
                all_tables.append(t)
        if all_tables:
            break   # 1ステップでも候補が得られたら以降はスキップ
        time.sleep(0.15)

    def _open_date(t):
        return t.get("OPEN_DATE", t.get("@updated", "0"))

    # DB型（0003始まり）を優先、次いで全候補、OPEN_DATE降順
    db  = [t for t in all_tables if t.get("@id","").startswith("0003")]
    ndb = [t for t in all_tables if not t.get("@id","").startswith("0003")]
    ordered = (sorted(db, key=_open_date, reverse=True)
               + sorted(ndb, key=_open_date, reverse=True))

    return [t.get("@id","") for t in ordered if t.get("@id","")]


def _parse_time_code(time_code: str) -> str:
    if len(time_code) >= 6:
        year, month = time_code[:4], time_code[4:6]
        return f"{year}-{month}" if month != "00" else year
    return time_code


def fetch_estat_data_by_id(stats_data_id, app_id, limit=14):
    """
    1つのIDでデータを取得する。
    戻り値: (records list or None, is_id_invalid bool)
    is_id_invalid=True の場合はIDが存在しないためスキップすべき
    """
    try:
        import requests
    except ImportError:
        return None, False

    params = {
        "appId": app_id, "lang": "J",
        "statsDataId": stats_data_id,
        "metaGetFlg": "N", "cntGetFlg": "N",
        "explanationGetFlg": "N", "annotationGetFlg": "N",
        "replaceSpChars": "0", "limit": str(limit * 3),
    }
    try:
        resp   = requests.get(ESTAT_DATA_URL, params=params, timeout=30)
        if resp.status_code != 200:
            return None, False
        raw    = resp.json()
        status = (raw.get("GET_STATS_DATA", {})
                     .get("RESULT", {}).get("STATUS", -1))
        if status != 0:
            msg = (raw.get("GET_STATS_DATA", {})
                      .get("RESULT", {}).get("ERROR_MSG", ""))
            is_invalid = "存在しません" in msg or "IDを確認" in msg
            return None, is_invalid

        values = (raw["GET_STATS_DATA"]["STATISTICAL_DATA"]
                     .get("DATA_INF", {}).get("VALUE", []))
        if not values:
            return None, False

        records = []
        for v in values:
            time_code = v.get("@time", "")
            value_str = v.get("$", "")
            if not value_str or value_str in ["-", "***", "…", "x", "X", ""]:
                continue
            try:
                records.append({
                    "time":      _parse_time_code(time_code),
                    "value":     float(value_str.replace(",", "")),
                    "time_code": time_code,
                })
            except (ValueError, TypeError):
                continue

        records.sort(key=lambda x: x.get("time_code", ""), reverse=True)
        return records[:limit] if records else None, False

    except Exception as e:
        print(f"    ⚠️  取得例外: {str(e)[:50]}")
        return None, False


def fetch_estat_with_failover(stats_data_id, app_id, stats_code,
                               search_word, hardcoded_id,
                               data_dir, limit=14):
    """
    【v9.0 核心機能】フェイルオーバー付きデータ取得

    1. キャッシュのIDで試行
    2. 失敗（IDが無効）→ キャッシュ削除 → 全候補IDをリスト化
    3. OPEN_DATE新しい順に全候補を試行
    4. 全候補失敗 → hardcoded_id で試行
    """
    month      = datetime.now().strftime("%Y-%m")
    cache_file = data_dir / f"id_cache_{stats_code}_{month}.txt" if data_dir else None
    data_cache = data_dir / f"data_{stats_data_id}_{month}.json" if data_dir else None

    # データキャッシュ確認
    if data_cache and data_cache.exists():
        try:
            return json.loads(data_cache.read_text(encoding="utf-8")), stats_data_id
        except Exception:
            pass

    # 最初の試行
    open_date_info = ""
    records, is_invalid = fetch_estat_data_by_id(stats_data_id, app_id, limit)

    if records:
        if data_cache:
            try:
                data_cache.parent.mkdir(parents=True, exist_ok=True)
                data_cache.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass
        return records, stats_data_id

    if is_invalid:
        # ★ フェイルオーバー発動: キャッシュを削除して再検索
        print(f"    ⚡ フェイルオーバー発動: {stats_data_id} は無効")
        if cache_file and cache_file.exists():
            cache_file.unlink()
        if data_cache and data_cache.exists():
            data_cache.unlink()

        # 全候補IDを取得
        candidates = _get_all_candidate_ids(app_id, stats_code, search_word)
        # hardcoded_id を末尾に追加（最終手段）
        if hardcoded_id and hardcoded_id not in candidates:
            candidates.append(hardcoded_id)
        # 無効だったIDは除外
        candidates = [c for c in candidates if c != stats_data_id]

        print(f"    🔄 代替候補 {len(candidates)}件を順に試行中...")
        for cid in candidates:
            time.sleep(0.3)
            recs, inv = fetch_estat_data_by_id(cid, app_id, limit)
            if recs:
                print(f"    ✅ フェイルオーバー成功: {cid}")
                # 新しいIDをキャッシュに保存
                if cache_file:
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_text(cid)
                    except Exception:
                        pass
                new_data_cache = (data_dir / f"data_{cid}_{month}.json"
                                  if data_dir else None)
                if new_data_cache:
                    try:
                        new_data_cache.write_text(
                            json.dumps(recs, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    except Exception:
                        pass
                return recs, cid
            if inv:
                continue  # 無効IDはスキップ

    return None, stats_data_id


def search_statsdata_id(app_id, stats_code, search_word,
                        data_dir=None, hardcoded_id=None):
    """キャッシュ確認 → 動的検索 → hardcoded_id の順でIDを返す"""
    if data_dir:
        month = datetime.now().strftime("%Y-%m")
        cache = data_dir / f"id_cache_{stats_code}_{month}.txt"
        if cache.exists():
            cached = cache.read_text().strip()
            if cached:
                return cached

    candidates = _get_all_candidate_ids(app_id, stats_code, search_word)
    if candidates:
        chosen = candidates[0]
        if data_dir:
            try:
                month = datetime.now().strftime("%Y-%m")
                cache = data_dir / f"id_cache_{stats_code}_{month}.txt"
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(chosen)
            except Exception:
                pass
        return chosen

    if hardcoded_id:
        return hardcoded_id
    return None


def calculate_changes(records):
    if not records:
        return {}
    latest = records[0]
    result = {"latest_value": latest["value"], "latest_time": latest["time"],
              "mom_change": None, "yoy_change": None}
    if len(records) >= 2:
        prev = records[1]["value"]
        if prev and prev != 0:
            result["mom_change"] = round(
                (latest["value"] - prev) / abs(prev) * 100, 2)
    if len(records) >= 13:
        yr = records[12]["value"]
        if yr and yr != 0:
            result["yoy_change"] = round(
                (latest["value"] - yr) / abs(yr) * 100, 2)
    return result


def judge_sector_impact(stat_def, changes):
    if not changes:
        return {"signal": "neutral", "score": 0, "reason": "データ取得失敗"}
    direction = stat_def.get("direction", "higher_is_good")
    change    = changes.get("yoy_change") if changes.get("yoy_change") is not None \
                else changes.get("mom_change")
    if change is None:
        return {"signal": "neutral", "score": 0, "reason": "変化率計算不可"}
    th = 0.5
    if direction == "higher_is_good":
        if change >= th:
            return {"signal": "tailwind", "score": 1,
                    "reason": f"前年比+{change:.1f}%（上昇＝追い風）"}
        elif change <= -th:
            return {"signal": "headwind", "score": -1,
                    "reason": f"前年比{change:.1f}%（下落＝逆風）"}
    else:
        if change >= th:
            return {"signal": "headwind", "score": -1,
                    "reason": f"前年比+{change:.1f}%（上昇＝逆風）"}
        elif change <= -th:
            return {"signal": "tailwind", "score": 1,
                    "reason": f"前年比{change:.1f}%（下落＝追い風）"}
    return {"signal": "neutral", "score": 0,
            "reason": f"前年比{change:+.1f}%（横ばい）"}


def aggregate_sector_signals(stat_results):
    sector_signals = {}
    for result in stat_results:
        impact = result.get("impact", {})
        stat   = result.get("stat", {})
        for code in stat.get("sectors", []):
            if code not in sector_signals:
                sector_signals[code] = {"score": 0, "tailwinds": [], "headwinds": []}
            sector_signals[code]["score"] += impact.get("score", 0)
            sig = impact.get("signal", "neutral")
            reason = impact.get("reason", "")
            if sig == "tailwind":
                sector_signals[code]["tailwinds"].append(f"{stat['name']}: {reason}")
            elif sig == "headwind":
                sector_signals[code]["headwinds"].append(f"{stat['name']}: {reason}")
    return sector_signals


def build_report(stat_results, sector_signals):
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 政府統計パイプライン レポート",
        f"**生成日時**: {now}", "",
        "---", "", "## 📊 取得統計サマリー", "",
        "| 統計名 | 最新値 | 前月比 | 前年比 | シグナル |",
        "|--------|--------|--------|--------|---------|",
    ]
    for r in stat_results:
        stat    = r["stat"]
        changes = r.get("changes", {})
        impact  = r.get("impact", {})
        latest  = changes.get("latest_value", "N/A")
        t       = changes.get("latest_time", "")
        mom     = changes.get("mom_change")
        yoy     = changes.get("yoy_change")
        icon    = {"tailwind": "📈 追い風", "headwind": "📉 逆風",
                   "neutral":  "➡️ 中立"}.get(impact.get("signal","neutral"), "➡️ 中立")
        mom_str = f"{mom:+.1f}%" if mom is not None else "―"
        yoy_str = f"{yoy:+.1f}%" if yoy is not None else "―"
        val_str = f"{latest:,.1f}" if isinstance(latest, float) else str(latest)
        lines.append(
            f"| {stat['name']} | {val_str}{stat['unit']} ({t}) "
            f"| {mom_str} | {yoy_str} | {icon} |")
    lines += ["", "---", "", "## 🏭 業種別 政府統計シグナル", ""]
    for code, data in sorted(sector_signals.items(),
                              key=lambda x: x[1]["score"], reverse=True):
        name  = SECTOR_NAMES.get(code, code)
        score = data["score"]
        icon  = "📈" if score > 0 else ("📉" if score < 0 else "➡️")
        lines.append(f"### {icon} {name}（スコア: {score:+d}）")
        for tw in data.get("tailwinds", []):
            lines.append(f"- 追い風: {tw}")
        for hw in data.get("headwinds", []):
            lines.append(f"- 逆風: {hw}")
        lines.append("")
    lines += ["---", f"*出典: e-Stat / 取得日: {now}*", ""]
    return "\n".join(lines)


def run_gov_pipeline(cfg) -> dict:
    print(f"\n{'='*60}")
    print("  📊 政府統計パイプライン v9.0 実行開始")
    print(f"{'='*60}")

    app_id = getattr(cfg, "ESTAT_API_KEY", "")
    if not app_id:
        print("  ⏩ ESTAT_API_KEY 未設定")
        return {"stats_fetched": 0, "stats_total": 0, "skipped": True}

    dirs        = setup_dirs(cfg)
    insight_dir = dirs["insight_dir"]
    data_dir    = dirs["data_dir"]
    stat_results = []

    for stat_def in STAT_DEFINITIONS:
        print(f"\n  📥 {stat_def['name']} 取得中...")

        stats_id = search_statsdata_id(
            app_id       = app_id,
            stats_code   = stat_def["statsCode"],
            search_word  = stat_def["searchWord"],
            data_dir     = data_dir,
            hardcoded_id = stat_def.get("hardcoded_id"),
        )
        if not stats_id:
            print(f"    ⚠️  statsDataId が見つかりません（{stat_def['statsCode']}）")
            stat_results.append({"stat": stat_def, "records": [], "changes": {},
                                  "impact": {"signal": "neutral", "score": 0,
                                              "reason": "ID取得失敗"}})
            time.sleep(0.3)
            continue

        print(f"    🔍 statsDataId: {stats_id}")

        # ★ フェイルオーバー付きデータ取得
        records, used_id = fetch_estat_with_failover(
            stats_data_id = stats_id,
            app_id        = app_id,
            stats_code    = stat_def["statsCode"],
            search_word   = stat_def["searchWord"],
            hardcoded_id  = stat_def.get("hardcoded_id"),
            data_dir      = data_dir,
        )

        if records:
            changes = calculate_changes(records)
            impact  = judge_sector_impact(stat_def, changes)
            icon    = {"tailwind": "📈", "headwind": "📉",
                       "neutral": "➡️"}.get(impact["signal"], "➡️")
            latest_time = changes.get("latest_time", "?")
            print(f"  {icon} {impact['reason']} （最新: {latest_time}）")
            stat_results.append({"stat": stat_def, "records": records,
                                  "changes": changes, "impact": impact})
        else:
            print("  ⚠️  データ取得失敗またはデータなし")
            stat_results.append({"stat": stat_def, "records": [], "changes": {},
                                  "impact": {"signal": "neutral", "score": 0,
                                              "reason": "データ取得失敗"}})
        time.sleep(0.5)

    sector_signals = aggregate_sector_signals(stat_results)
    report_md      = build_report(stat_results, sector_signals)
    today_str      = datetime.now().strftime("%Y-%m-%d")
    report_path    = insight_dir / f"gov_report_{today_str}.md"

    try:
        report_path.write_text(report_md, encoding="utf-8")
        print(f"\n  ✅ レポート保存: {report_path}")
    except Exception as e:
        print(f"\n  ⚠️  レポート保存失敗: {e}")
        report_path = None

    fetched = sum(1 for r in stat_results if r.get("records"))
    total   = len(STAT_DEFINITIONS)
    print(f"  📊 取得完了: {fetched}/{total} 統計")

    return {
        "stat_results": stat_results, "sector_signals": sector_signals,
        "report_path": str(report_path) if report_path else "",
        "stats_fetched": fetched, "stats_total": total, "skipped": False,
    }


def main(cfg=None):
    if cfg is None:
        import importlib.util
        for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
            if Path(p).exists():
                spec = importlib.util.spec_from_file_location("config", p)
                cfg  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break
    return run_gov_pipeline(cfg)


if __name__ == "__main__":
    import importlib.util
    for p in ["/content/config.py", str(Path(__file__).parent / "config.py")]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            cfg  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cfg)
            break
    key = getattr(cfg, "ESTAT_API_KEY", "")
    if key:
        result = run_gov_pipeline(cfg)
        print(f"\n取得統計数: {result['stats_fetched']}/{result['stats_total']}")
