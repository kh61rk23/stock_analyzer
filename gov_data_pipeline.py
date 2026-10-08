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
    },
    {
        "id":           "industrial_production",
        "name":         "鉱工業生産指数",
        "statsCode":    "00550300",
        "searchWord":   "鉱工業",
        "unit":         "（前年比%）",
        # e-Stat の統計表IDは改定のたびに変わり追いきれないため、
        # 系列IDが固定の FRED（OECD経由の公式統計）から取る。
        "fred":         {"series": "JPNPRINTO01GYSAM", "kind": "yoy"},
        "direction":    "higher_is_good",
        "sectors":      ["1015", "1016", "1007", "1012"],
        "description":  "生産指数の上昇は製造業全般の追い風",
    },
    {
        "id":           "wages",
        "name":         "毎月勤労統計（現金給与総額）",
        "statsCode":    "00450071",
        "searchWord":   "現金給与総額",
        "unit":         "（前年比%）",
        "fred":         {"series": "LCEAMN01JPM659S", "kind": "yoy"},
        "direction":    "higher_is_good",
        "sectors":      ["1027", "1033", "1004", "1019"],
        "description":  "賃金上昇は消費拡大につながり、小売・外食・サービス業に追い風",
        # v9.1: フォールバックIDを設定（動的検索が2018年IDを掴む問題への対処）
        # 動的検索でも _is_stale チェックでフェイルオーバーするが、IDがNoneだと最終手段なし
    },
    {
        "id":           "household_spending",
        "name":         "小売販売数量（家計消費の代理）",
        "statsCode":    "00200561",
        "searchWord":   "消費支出",
        "unit":         "（前年比%）",
        # 家計調査の消費支出は FRED に現役の系列が無いため、小売販売数量で代替する
        "fred":         {"series": "JPNSLRTTO01GYSAM", "kind": "yoy"},
        "direction":    "higher_is_good",
        "sectors":      ["1027", "1033", "1004", "1026"],
        "description":  "消費支出の増加は内需型業種全般の追い風",
    },
    {
        "id":           "unemployment",
        "name":         "労働力調査（完全失業率）",
        "statsCode":    "00200531",
        "searchWord":   "完全失業率 月次",
        "unit":         "（%）",
        "fred":         {"series": "LRUN64TTJPM156S", "kind": "level"},
        "direction":    "higher_is_bad",
        "sectors":      ["1027", "1033", "1028", "1032"],
        "description":  "失業率上昇は景気悪化を示し、内需・金融・不動産に逆風",
    },
    {
        "id":           "construction_starts",
        "name":         "建設工事着工統計",
        "statsCode":    "00600130",   # 国交省 建設着工統計調査（v9.1修正: 00600220→00600130）
        "searchWord":   "新設住宅着工",
        "unit":         "（戸）",
        "direction":    "higher_is_good",
        "sectors":      ["1003", "1032", "1012", "1014"],
        "description":  "着工件数の増加は建設・不動産・鉄鋼の追い風",
        "fallback_stats_codes": ["00600220", "00600140"],  # 複数statsCodeを試す
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
    },
    {
        "id":           "trade_exports",
        "name":         "貿易統計（輸出総額）",
        "statsCode":    "00350300",
        "searchWord":   "輸出",
        "unit":         "（前年比%）",
        "fred":         {"series": "XTEXVA01JPM659S", "kind": "yoy"},
        "direction":    "higher_is_good",
        "sectors":      ["1017", "1015", "1007", "1016"],
        "description":  "輸出増加は外需型製造業（自動車・機械・化学）の追い風",
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

    # OPEN_DATE降順のみで並べる（ID prefix で優先しない）。
    # 理由: prefix 優先にすると 0004-prefixの正しいIDが0003-prefixの古いIDに負ける
    ordered = sorted(all_tables, key=_open_date, reverse=True)
    return [t.get("@id","") for t in ordered if t.get("@id","")]


def _parse_time_code(time_code: str) -> str:
    # e-Statの標準時間コードは10桁 "YYYY00MMMM"（月次）/
    # "YYYY000000"（年次）/ "YYYY100000"（年度）。[4:6]="00"を月と読むと
    # 月次データが年だけになり、前年比が別の月どうしの比較になって異常値が出る。
    if len(time_code) == 10 and time_code[:4].isdigit() and time_code[4:6] in ("00", "10"):
        year, month = time_code[:4], time_code[8:10]
        return f"{year}-{month}" if month not in ("00",) else year
    if len(time_code) >= 6:
        year, month = time_code[:4], time_code[4:6]
        return f"{year}-{month}" if month != "00" else year
    return time_code


def _is_stale(records, max_years_old=2) -> bool:
    """最新データが max_years_old 年以上前なら True（古いIDを検出するため）"""
    if not records:
        return False
    latest_time = records[0].get("time", "")
    if not latest_time:
        return False
    try:
        year = int(latest_time[:4])
        return (datetime.now().year - year) >= max_years_old
    except (ValueError, IndexError):
        return False


def fetch_estat_data_by_id(stats_data_id, app_id, limit=24):
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
            # BUG-02: 失敗を握り潰さず、
            # HTTPステータスとIDを必ずログに残す（実行台帳/Actionsログで追跡するため）
            print(f"    ⚠️  [gov_data] e-Stat HTTP {resp.status_code}: "
                  f"statsDataId={stats_data_id}")
            return None, False
        raw    = resp.json()
        status = (raw.get("GET_STATS_DATA", {})
                     .get("RESULT", {}).get("STATUS", -1))
        if status != 0:
            msg = (raw.get("GET_STATS_DATA", {})
                      .get("RESULT", {}).get("ERROR_MSG", ""))
            is_invalid = "存在しません" in msg or "IDを確認" in msg
            # BUG-02 診断強化: APIレベルのエラー内容を可視化
            print(f"    ⚠️  [gov_data] e-Stat STATUS={status} "
                  f"statsDataId={stats_data_id}: {msg[:80]}")
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
                # カテゴリ属性（@cat01/@area/@tab等）を保持する。
                #   e-Stat応答は複数カテゴリの行が混在するため、時系列比較は
                #   同一カテゴリ内で行う必要がある（下の系列選択で使用）。
                cat_key = tuple(sorted(
                    (k, val) for k, val in v.items()
                    if k.startswith("@") and k not in ("@time", "@unit")
                ))
                records.append({
                    "time":      _parse_time_code(time_code),
                    "value":     float(value_str.replace(",", "")),
                    "time_code": time_code,
                    "_cat":      cat_key,
                })
            except (ValueError, TypeError):
                continue

        if not records:
            return None, False

        # 異種カテゴリの値を1本の時系列として前年比計算すると異常値になるため、
        #   最新時点を含むカテゴリ系列のうち最も行数の多い1系列だけを採用する
        #   （「総数・計」に相当する系列が最長になるのが通例）。
        by_cat = {}
        for r in records:
            by_cat.setdefault(r["_cat"], []).append(r)
        latest_code = max(r["time_code"] for r in records)
        candidates = {k: rs for k, rs in by_cat.items()
                      if any(r["time_code"] == latest_code for r in rs)} or by_cat
        best = max(candidates.values(), key=len)
        records = [{k: v2 for k, v2 in r.items() if k != "_cat"} for r in best]

        records.sort(key=lambda x: x.get("time_code", ""), reverse=True)
        return records[:limit] if records else None, False

    except Exception as e:
        print(f"    ⚠️  取得例外: {str(e)[:50]}")
        return None, False


_FAILOVER_MAX_CANDIDATES = 15


def fetch_estat_with_failover(stats_data_id, app_id, stats_code,
                               search_word, hardcoded_id,
                               data_dir, limit=24):
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

    good_file  = data_dir / f"failover_id_{stats_code}.txt" if data_dir else None

    # データキャッシュ確認（陳腐化した内容は使わずフェイルオーバーへ回す）
    if data_cache and data_cache.exists():
        try:
            cached = json.loads(data_cache.read_text(encoding="utf-8"))
            for r in cached:   # 旧形式で保存された time を time_code から引き直す
                if r.get("time_code"):
                    r["time"] = _parse_time_code(r["time_code"])
            if not _is_stale(cached):
                return cached, stats_data_id
        except Exception:
            pass

    # 最初の試行
    records, is_invalid = fetch_estat_data_by_id(stats_data_id, app_id, limit)

    # records があっても陳腐化していれば return しない。ここで即 return すると
    # 下の陳腐化フェイルオーバーが一度も発動しなくなる。
    if records and not _is_stale(records):
        if data_cache:
            try:
                data_cache.parent.mkdir(parents=True, exist_ok=True)
                data_cache.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass
        return records, stats_data_id

    # 古いデータ（2年以上前）もフェイルオーバー対象
    is_stale = _is_stale(records)
    if is_stale and records:
        print(f"    ⚡ フェイルオーバー発動: {stats_data_id} は古いデータ（最新: {records[0].get('time','?')}）")

    exhausted = (data_dir / f"failover_exhausted_{stats_code}_{month}.txt"
                 if data_dir else None)
    # 応答はあるが値が空（例: 家計調査 0002070001 が毎日「データなし」）の
    # ときも、無効ID・陳腐化と同様に代替を探す。
    is_empty = records is None and not is_invalid
    if (is_stale or is_empty) and not is_invalid and exhausted and exhausted.exists():
        # 今月すでに全候補を試して新しい表が無かった → 毎日の再探索を省く
        return records, stats_data_id

    if is_invalid or is_stale or is_empty:
        # フェイルオーバー発動: キャッシュを削除して再検索
        if is_invalid:
            print(f"    ⚡ フェイルオーバー発動: {stats_data_id} は無効")
        elif is_empty:
            print(f"    ⚡ フェイルオーバー発動: {stats_data_id} はデータが空")
        if cache_file and cache_file.exists():
            cache_file.unlink()
        if data_cache and data_cache.exists():
            data_cache.unlink()

        # 全候補IDを取得
        candidates = _get_all_candidate_ids(app_id, stats_code, search_word)
        # hardcoded_idを先頭に挿入（最優先で試行）
        # 理由: hardcoded_idは人間が確認済みの正しいID。末尾より先頭に置く
        if hardcoded_id and hardcoded_id not in candidates:
            candidates.insert(0, hardcoded_id)
        elif hardcoded_id and hardcoded_id in candidates:
            candidates.remove(hardcoded_id)
            candidates.insert(0, hardcoded_id)
        # 前回フェイルオーバーで当たったIDを最優先で試す
        prev_good = None
        if good_file and good_file.exists():
            try:
                prev_good = good_file.read_text().strip() or None
            except OSError:
                prev_good = None
        if prev_good:
            if prev_good in candidates:
                candidates.remove(prev_good)
            candidates.insert(0, prev_good)
        # 無効だったIDは除外し、試行数に上限を設ける（1候補≒1秒）
        candidates = [c for c in candidates if c != stats_data_id][:_FAILOVER_MAX_CANDIDATES]

        print(f"    🔄 代替候補 {len(candidates)}件を順に試行中...")
        for cid in candidates:
            time.sleep(0.3)
            recs, inv = fetch_estat_data_by_id(cid, app_id, limit)
            if recs and _is_stale(recs):
                continue  # 候補も古い表なら採用しない
            if recs:
                print(f"    ✅ フェイルオーバー成功: {cid}")
                if good_file:
                    try:
                        good_file.write_text(cid)
                    except OSError:
                        pass
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
        print(f"    ❌ 代替候補に新しいデータなし（{stats_code} / 要 hardcoded_id 更新）")
        if exhausted:
            try:
                exhausted.write_text(datetime.now().strftime("%Y-%m-%d"))
            except OSError:
                pass

    # 代替が無ければ古いデータのまま返す（judge_sector_impact の鮮度ガードで無効化される）
    return records, stats_data_id

def search_statsdata_id(app_id, stats_code, search_word, data_dir=None, hardcoded_id=None):
    """
    動的検索を最優先し、最新のIDを特定する。
    """
    # 候補IDをAPIから取得（更新日時が新しい順に並んでいる）
    candidates = _get_all_candidate_ids(app_id, stats_code, search_word)
    
    if candidates:
        chosen = candidates[0] # 更新日が一番新しいIDを採用
        if data_dir:
            try:
                month = datetime.now().strftime("%Y-%m")
                cache = data_dir / f"id_cache_{stats_code}_{month}.txt"
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(chosen)
            except Exception:
                pass
        return chosen

    # 動的検索で見つからなかった場合の最終手段としてのみ固定IDを使う
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
    # 1年前は時刻文字列で特定する（records[12] 固定にしない）
    # 理由: データが月次でない場合records[12]が12年前等になり異常値
    try:
        latest_time = latest.get("time", "")
        latest_year = int(latest_time[:4]) if latest_time else 0
        target_year = str(latest_year - 1) if latest_year > 0 else ""
        yr_record   = None
        if target_year:
            yr_record = next(
                (r for r in records[1:] if r["time"].startswith(target_year)),
                None
            )
        # 月次なら同月（"2025-07"）と比べる。年だけの一致だと先頭＝12月を拾う
        if len(latest_time) >= 7 and target_year:
            same_month = f"{target_year}{latest_time[4:7]}"
            yr_record = next(
                (r for r in records[1:] if r["time"] == same_month), None)
        if yr_record:
            yr = yr_record["value"]
            if yr and yr != 0:
                result["yoy_change"] = round(
                    (latest["value"] - yr) / abs(yr) * 100, 2)
    except Exception:
        pass
    return result


def judge_sector_impact(stat_def, changes):
    if not changes:
        return {"signal": "neutral", "score": 0, "reason": "データ取得失敗"}
    # 鮮度ガード: フェイルオーバーを尽くしても最新が2年以上前ならシグナル化しない。
    #   e-Statの旧固定表を掴むと何年も前の前年比が MACRO_RULES へ流入するため。
    latest_time = str(changes.get("latest_time", ""))
    try:
        _yr = int(latest_time[:4])
    except (ValueError, TypeError):
        _yr = 0
    if _yr and (datetime.now().year - _yr) >= 2:
        return {"signal": "neutral", "score": 0,
                "reason": f"データ陳腐化（最新{latest_time}）のため無効・要ID更新"}
    direction = stat_def.get("direction", "higher_is_good")
    change    = changes.get("yoy_change") if changes.get("yoy_change") is not None \
                else changes.get("mom_change")
    if change is None:
        return {"signal": "neutral", "score": 0, "reason": "変化率計算不可"}
    # 異常値クランプ: マクロ統計の前年比が±50%を超えることは
    #   通常なく、カテゴリ混在・単位揺れ等のデータ品質問題を示す → 無効化。
    if abs(change) > 50:
        return {"signal": "neutral", "score": 0,
                "reason": f"変化率異常（{change:+.1f}%・データ品質疑い）のため無効"}
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
        # score は impact.get("score", 0) の累積で float になり得るため、
        # 整数書式 :+d は使えない（"Unknown format code 'd' for object of type
        # 'float'" でレポート生成ごと例外＝gov pipeline 全体が False を返し、
        # 下流 B-2 が results["gov"] の bool で連鎖クラッシュしていた）。
        # :+.0f なら int/float どちらでも符号付き整数表示になる。
        lines.append(f"### {icon} {name}（スコア: {score:+.0f}）")
        for tw in data.get("tailwinds", []):
            lines.append(f"- 追い風: {tw}")
        for hw in data.get("headwinds", []):
            lines.append(f"- 逆風: {hw}")
        lines.append("")
    lines += ["---", f"*出典: e-Stat / 取得日: {now}*", ""]
    return "\n".join(lines)

# A-1 貿易品目別シグナル・A-2 職種別求人倍率シグナルは廃止済み


# ==============================================================
# FRED 取得
# ==============================================================
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_STALE_MONTHS = 9   # 最新がこれより古ければ「系列の更新停止」とみなす


def fetch_fred_series(series_id: str, api_key: str = "", limit: int = 36):
    """FRED の月次系列を新しい順の records で返す（失敗時は None と理由）。

    APIキーがあれば公式API、無ければ公開CSV（キー不要）を使う。
    Returns: (records | None, error_str)
    """
    try:
        import requests
    except ImportError:
        return None, "requests なし"
    rows = []
    try:
        if api_key:
            r = requests.get(FRED_API_URL, params={
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "sort_order": "desc", "limit": str(limit)}, timeout=30)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            for o in r.json().get("observations", []):
                rows.append((o.get("date", ""), o.get("value", "")))
        else:
            r = requests.get(FRED_CSV_URL, params={"id": series_id}, timeout=30)
            if r.status_code != 200 or "," not in r.text[:200]:
                return None, f"HTTP {r.status_code}"
            for line in r.text.strip().splitlines()[1:]:
                d, _, v = line.partition(",")
                rows.append((d, v))
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:60]}"
    records = []
    for d, v in rows:
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue   # FRED は欠測を "." で返す
        records.append({"time": d[:7], "value": val, "time_code": d[:7].replace("-", "")})
    records.sort(key=lambda x: x["time_code"], reverse=True)
    return (records[:limit] or None), ("" if records else "データなし")


def fred_changes(records: list, kind: str) -> dict:
    """FRED 系列から calculate_changes と同じ形の dict を作る（純関数）。

    kind="yoy"   系列そのものが前年比(%)。値をそのまま yoy_change に使う
    kind="level" 水準（失業率など）。calculate_changes で前年同月比を出す
    """
    if not records:
        return {}
    if kind == "yoy":
        latest = records[0]
        prev = records[1]["value"] if len(records) > 1 else None
        return {"latest_value": latest["value"], "latest_time": latest["time"],
                "yoy_change": round(latest["value"], 2),
                "mom_change": round(latest["value"] - prev, 2) if prev is not None else None}
    return calculate_changes(records)


def _months_old(ym: str) -> int:
    try:
        y, m = int(ym[:4]), int(ym[5:7])
    except (ValueError, IndexError):
        return 999
    now = datetime.now()
    return (now.year - y) * 12 + (now.month - m)


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

    fred_key = getattr(cfg, "POLICY_FRED_API_KEY", "") or ""
    for stat_def in STAT_DEFINITIONS:
        print(f"\n  📥 {stat_def['name']} 取得中...")

        # fred を持つ統計は e-Stat を使わず FRED から取る
        if stat_def.get("fred"):
            sid, kind = stat_def["fred"]["series"], stat_def["fred"].get("kind", "level")
            records, err = fetch_fred_series(sid, fred_key)
            print(f"    🔍 FRED: {sid}（{'API' if fred_key else 'CSV'}）")
            if not records:
                print(f"  ⚠️  FRED取得失敗: {err}")
                stat_results.append({"stat": stat_def, "records": [], "changes": {},
                                     "impact": {"signal": "neutral", "score": 0,
                                                "reason": f"FRED取得失敗（{err}）"}})
                continue
            age = _months_old(records[0]["time"])
            if age > FRED_STALE_MONTHS:
                # 系列が止まった（FRED側の打ち切り）ことを隠さない。シグナル化しない
                print(f"  ⚠️  FRED系列 {sid} の更新が止まっている可能性"
                      f"（最新 {records[0]['time']}・{age}か月前）→ 系列の差し替えが必要")
                stat_results.append({"stat": stat_def, "records": records, "changes": {},
                                     "impact": {"signal": "neutral", "score": 0,
                                                "reason": f"FRED系列の更新停止（最新{records[0]['time']}）"}})
                continue
            changes = fred_changes(records, kind)
            impact = judge_sector_impact(stat_def, changes)
            icon = {"tailwind": "📈", "headwind": "📉", "neutral": "➡️"}.get(impact["signal"], "➡️")
            print(f"  {icon} {impact['reason']} （最新: {changes.get('latest_time', '?')}）")
            stat_results.append({"stat": stat_def, "records": records,
                                 "changes": changes, "impact": impact})
            time.sleep(0.3)
            continue

        # statsCode を複数候補で試す（fallback_stats_codes対応）
        all_codes = [stat_def["statsCode"]] + stat_def.get("fallback_stats_codes", [])
        stats_id = None
        for _try_code in all_codes:
            stats_id = search_statsdata_id(
                app_id       = app_id,
                stats_code   = _try_code,
                search_word  = stat_def["searchWord"],
                data_dir     = data_dir,
                hardcoded_id = stat_def.get("hardcoded_id") if _try_code == all_codes[0] else None,
            )
            if stats_id:
                if _try_code != stat_def["statsCode"]:
                    print(f"    🔄 フォールバックstatsCode使用: {_try_code}")
                break
        if not stats_id:
            print(f"    ⚠️  statsDataId が見つかりません（{' / '.join(all_codes)}）")
            stat_results.append({"stat": stat_def, "records": [], "changes": {},
                                  "impact": {"signal": "neutral", "score": 0,
                                              "reason": "ID取得失敗"}})
            time.sleep(0.3)
            continue

        print(f"    🔍 statsDataId: {stats_id}")

        # フェイルオーバー付きデータ取得
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
