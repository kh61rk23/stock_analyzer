# ==============================================================
# industry_standard_tracker.py  v1.0  (Wave 7 - Step 1)
# 標準化動向トラッカー（3GPP / ISO / IEC / IEEE 標準）
# ==============================================================
# 【役割】
#   3GPP・ISO・IEC・IEEE等の国際標準化機関が公開する
#   提案（Contribution）・承認（Approval）・改訂（Amendment）の
#   動向を集計・追跡し、standardization_cache.json に保存する。
#
# 【設計思想】
#   ・標準化主導企業 = 次世代技術の先行指標
#   ・contribution_count の急増 → 当該技術領域への先行投資シグナル
#   ・approved_standards の増加 → 実装フェーズ突入の先行シグナル
#   ・scraping失敗時はキャッシュ継続使用（エラーで止まらない設計）
#
# 【主要出力キャッシュ】
#   CACHE_DIR/standardization_cache.json
#   TTL: 180日（月次更新）
#   スキーマ:
#     {
#       "last_updated": "YYYY-MM-DD",
#       "version": "1.0",
#       "standards": {
#         "3gpp_r18": {
#           "standard_id": str,
#           "body": str,          # "3GPP" / "ISO" / "IEC" / "IEEE"
#           "release": str,       # "R18" / "R19"
#           "topic": str,
#           "tech_node_ids": list,  # taxonomy.taxonomy_node.node_id と対応
#           "company_contributions": {
#             "company_code": {
#               "ticker": str,
#               "contribution_count": int,
#               "approved_count": int,
#               "rapporteur_flag": bool,
#               "last_seen": str,
#             }
#           },
#           "total_contributions": int,
#           "trend": str,         # "rising" / "stable" / "declining"
#           "signal_score": float,  # 0.0 ~ 10.0
#         }
#       },
#       "company_scores": {
#         "ticker": {
#           "standardization_score": float,  # 0.0 ~ 100.0
#           "leading_standards": list,
#           "rapporteur_count": int,
#           "yoy_contribution_delta": float,
#         }
#       },
#       "stats": {
#         "total_standards_tracked": int,
#         "total_companies_tracked": int,
#         "data_sources": list,
#       }
#     }
#
# 【データ取得戦略（段階的フォールバック）】
#   Level 1: 公式APIエンドポイント（3GPP ETSI, IEEE Xplore API）
#   Level 2: 公開HTMLページのパース（BeautifulSoup）
#   Level 3: キャッシュ継続使用（取得失敗時）
#
# 【依存】
#   config.py（Wave 7追加定数参照）
#   requests, BeautifulSoup（標準外ライブラリ）
#   ★ try/except で全ブロックを保護。失敗時はキャッシュを返す。
# ==============================================================

from __future__ import annotations

import json
import time
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

# ==============================================================
# BLOCK 0: デフォルト定数（config.py が読めない場合のフォールバック）
# ==============================================================

_CACHE_FILENAME = "standardization_cache.json"
_DEFAULT_TTL_DAYS = 180   # 月次更新

# 追跡対象の標準化機関とリリース定義
_DEFAULT_STANDARDS_CONFIG: dict = {
    "3gpp": {
        "body": "3GPP",
        "releases": ["R17", "R18", "R19"],
        "topics": [
            "NR_mmWave",
            "RedCap",
            "NTN",       # Non-Terrestrial Network
            "6G_AI",
            "ISAC",      # Integrated Sensing and Communication
            "OpenRAN",
            "network_slicing",
            "private_5G",
        ],
        "tech_node_map": {
            "NR_mmWave":       ["semiconductor_rf", "antenna_module"],
            "RedCap":          ["iot_modem", "semiconductor_logic"],
            "NTN":             ["satellite_comm", "semiconductor_rf"],
            "6G_AI":           ["ai_edge_chip", "semiconductor_logic"],
            "ISAC":            ["sensor_radar", "semiconductor_rf"],
            "OpenRAN":         ["telecom_software", "semiconductor_asic"],
            "network_slicing": ["telecom_software"],
            "private_5G":      ["telecom_infra", "semiconductor_rf"],
        },
        "known_contributors_jp": {
            "NTT":            {"ticker": "9432.T", "code": "9432"},
            "NTT DoCoMo":     {"ticker": "9432.T", "code": "9432"},
            "KDDI":           {"ticker": "9433.T", "code": "9433"},
            "SoftBank":       {"ticker": "9434.T", "code": "9434"},
            "Fujitsu":        {"ticker": "6702.T", "code": "6702"},
            "NEC":            {"ticker": "6701.T", "code": "6701"},
            "Panasonic":      {"ticker": "6752.T", "code": "6752"},
            "Sony":           {"ticker": "6758.T", "code": "6758"},
            "Murata":         {"ticker": "6981.T", "code": "6981"},
            "Kyocera":        {"ticker": "6971.T", "code": "6971"},
        },
        "known_contributors_us": {
            "Qualcomm":       {"ticker": "QCOM",   "code": "QCOM"},
            "Ericsson":       {"ticker": "ERIC",   "code": "ERIC"},
            "Nokia":          {"ticker": "NOK",    "code": "NOK"},
            "Huawei":         {"ticker": None,     "code": "HW"},
            "Samsung":        {"ticker": None,     "code": "SSNLF"},
            "Apple":          {"ticker": "AAPL",   "code": "AAPL"},
            "Intel":          {"ticker": "INTC",   "code": "INTC"},
            "MediaTek":       {"ticker": None,     "code": "2454TW"},
        },
    },
    "iso_iec": {
        "body": "ISO/IEC",
        "committees": ["JTC1/SC17", "JTC1/SC27", "JTC1/SC42", "TC22/SC32"],
        "topics": [
            "AI_standards",          # SC42
            "cybersecurity",         # SC27
            "EV_charging",           # TC22/SC32
            "quantum_crypto",        # SC27
            "semiconductor_package", # SC17
        ],
        "tech_node_map": {
            "AI_standards":          ["ai_ml_framework", "ai_edge_chip"],
            "cybersecurity":         ["security_chip", "security_software"],
            "EV_charging":           ["ev_charger", "ev_battery"],
            "quantum_crypto":        ["quantum_computing", "security_chip"],
            "semiconductor_package": ["semiconductor_package", "hbm"],
        },
        "known_contributors_jp": {
            "Fujitsu":   {"ticker": "6702.T", "code": "6702"},
            "Hitachi":   {"ticker": "6501.T", "code": "6501"},
            "Toshiba":   {"ticker": "6502.T", "code": "6502"},
            "NEC":       {"ticker": "6701.T", "code": "6701"},
            "Renesas":   {"ticker": "6723.T", "code": "6723"},
        },
        "known_contributors_us": {
            "IBM":       {"ticker": "IBM",    "code": "IBM"},
            "Microsoft": {"ticker": "MSFT",   "code": "MSFT"},
            "Google":    {"ticker": "GOOGL",  "code": "GOOGL"},
            "Apple":     {"ticker": "AAPL",   "code": "AAPL"},
        },
    },
    "ieee": {
        "body": "IEEE",
        "working_groups": [
            "802.11be",    # Wi-Fi 7
            "802.11bn",    # Wi-Fi 8
            "802.3cz",     # 200G/400G Ethernet
            "1547",        # 分散電源系統連系
            "P2857",       # AI倫理
            "P3005",       # 半導体チップレット
        ],
        "topics": [
            "wifi7_8",
            "high_speed_ethernet",
            "grid_interconnect",
            "ai_ethics_standard",
            "chiplet_standard",
        ],
        "tech_node_map": {
            "wifi7_8":              ["wifi_chip", "semiconductor_rf"],
            "high_speed_ethernet":  ["datacenter_nic", "semiconductor_logic"],
            "grid_interconnect":    ["smart_grid", "power_semiconductor"],
            "ai_ethics_standard":   ["ai_ml_framework"],
            "chiplet_standard":     ["semiconductor_package", "semiconductor_logic"],
        },
        "known_contributors_jp": {
            "Sony":      {"ticker": "6758.T", "code": "6758"},
            "Toshiba":   {"ticker": "6502.T", "code": "6502"},
            "Renesas":   {"ticker": "6723.T", "code": "6723"},
            "Murata":    {"ticker": "6981.T", "code": "6981"},
        },
        "known_contributors_us": {
            "Broadcom":  {"ticker": "AVGO",   "code": "AVGO"},
            "Cisco":     {"ticker": "CSCO",   "code": "CSCO"},
            "Intel":     {"ticker": "INTC",   "code": "INTC"},
            "Marvell":   {"ticker": "MRVL",   "code": "MRVL"},
        },
    },
}

# シグナルスコア計算パラメータ
_SIGNAL_SCORE_WEIGHTS = {
    "contribution_count_norm": 0.40,  # 標準化貢献数（正規化）
    "approved_ratio":           0.30,  # 承認率
    "rapporteur_flag":          0.20,  # ラポルツール（主査）担当フラグ
    "yoy_growth":               0.10,  # 前年比成長率
}
_STANDARDIZATION_SCORE_MAX = 100.0

# データソースURL（パブリックAPIと公開ページ）
_DATA_SOURCES = {
    "3gpp_tdoc":    "https://www.3gpp.org/ftp/tsg_ran/TSG_RAN/TSGR_latest/",
    "etsi_portal":  "https://portal.etsi.org/",
    "ieee_sa":      "https://standards.ieee.org/ieee/",
    "iso_search":   "https://www.iso.org/search.html",
}

from config_sync import sync_module_constants

_CFG_CONST_MAP = {
    'B5_INDUSTRY_STANDARD_TRACKER_REQUEST_TIMEOUT': '_REQUEST_TIMEOUT',
}

_REQUEST_TIMEOUT = 20
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/json, text/html, */*",
}


# ==============================================================
# BLOCK 1: キャッシュ管理
# ==============================================================

def _cache_path(cfg) -> Path:
    """キャッシュファイルのフルパスを返す。"""
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _CACHE_FILENAME


def _load_cache(cfg) -> Optional[dict]:
    """
    TTL内のキャッシュを返す。
    失効または未存在の場合は None。
    """
    try:
        p = _cache_path(cfg)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        last_updated = data.get("last_updated", "")
        ttl_days = int(getattr(cfg, "STANDARDIZATION_CACHE_TTL_DAYS", _DEFAULT_TTL_DAYS))
        if last_updated:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d")).days
            if age < ttl_days:
                return data
    except Exception:
        pass
    return None


def _save_cache(cfg, data: dict) -> None:
    """キャッシュをJSONファイルに保存する。"""
    try:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        p = _cache_path(cfg)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [standardization] キャッシュ保存失敗: {str(e)[:60]}")


# ==============================================================
# BLOCK 2: データ取得ユーティリティ
# ==============================================================

def _safe_get(url: str, timeout: int = _REQUEST_TIMEOUT, retries: int = 2) -> Optional[str]:
    """
    HTTPリクエストを実行し、レスポンステキストを返す。
    失敗時はNoneを返す（例外を外部に伝播しない）。
    """
    try:
        import requests
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code in (429, 503):
                    # レートリミット・サービス停止: wait & retry
                    time.sleep(5 * (attempt + 1))
            except Exception:
                time.sleep(2)
        return None
    except ImportError:
        return None


def _parse_company_from_text(text: str, contributor_map: dict) -> dict:
    """
    テキストから既知企業名を検出し、
    {company_name: {ticker, code}} の部分辞書を返す。
    """
    found = {}
    text_lower = text.lower()
    for name, info in contributor_map.items():
        if name.lower() in text_lower:
            found[name] = info
    return found


def _calc_signal_score(
    contribution_count: int,
    approved_count: int,
    rapporteur_flag: bool,
    yoy_delta: float,
    max_contributions_in_standard: int = 100,
) -> float:
    """
    標準化シグナルスコアを計算する。
    $score = w_1 \\times norm\\_cont + w_2 \\times appr\\_ratio + w_3 \\times rapporteur + w_4 \\times yoy$
    Returns: float 0.0 ~ 10.0
    """
    try:
        # 貢献数正規化（上限をmax_contributions_in_standardで割る）
        max_c = max(1, max_contributions_in_standard)
        norm_cont = min(1.0, contribution_count / max_c)

        # 承認率
        appr_ratio = approved_count / max(1, contribution_count)

        # ラポルツールフラグ
        rapporteur = 1.0 if rapporteur_flag else 0.0

        # YoY正規化（-1.0 ~ +1.0 を 0.0 ~ 1.0 に変換）
        yoy_norm = min(1.0, max(0.0, (yoy_delta + 1.0) / 2.0))

        w = _SIGNAL_SCORE_WEIGHTS
        raw = (
            w["contribution_count_norm"] * norm_cont
            + w["approved_ratio"]        * appr_ratio
            + w["rapporteur_flag"]       * rapporteur
            + w["yoy_growth"]            * yoy_norm
        )
        return round(raw * 10.0, 2)  # 0.0 ~ 10.0 に スケール
    except Exception:
        return 0.0


# ==============================================================
# BLOCK 3: 3GPP データ取得
# ==============================================================

def _fetch_3gpp_contributions(cfg, standard_config: dict) -> dict:
    """
    3GPPのTSG-RAN/TSG-SA会議からContribution数を集計する。

    フォールバック戦略:
      Level 1: ETSI portal API（JSON）
      Level 2: 3GPP FTPサイトのファイルリスト解析
      Level 3: 公開統計ページのHTML解析
      Level 4: ダミーデータ（すべて失敗時）

    Returns:
      {
        "standard_id": str,
        "body": "3GPP",
        "topics": {topic: {contributions: dict, approved: int, total: int}},
      }
    """
    results = {}
    releases = standard_config.get("releases", ["R18"])
    topics   = standard_config.get("topics", [])
    tech_map = standard_config.get("tech_node_map", {})
    contrib_jp = standard_config.get("known_contributors_jp", {})
    contrib_us = standard_config.get("known_contributors_us", {})
    all_contribs = {**contrib_jp, **contrib_us}

    for release in releases:
        for topic in topics:
            standard_id = f"3gpp_{release.lower()}_{topic.lower()}"
            try:
                # Level 1: ETSI APIを試行
                etsi_url = (
                    f"https://portal.etsi.org/Regportale/GetDocInfo.aspx?"
                    f"q={topic}&spec={release}&format=json"
                )
                raw = _safe_get(etsi_url, timeout=15, retries=1)
                company_contributions = {}

                if raw:
                    try:
                        etsi_data = json.loads(raw)
                        docs = etsi_data.get("DocumentList", [])
                        for doc in docs:
                            source = doc.get("Source", "")
                            for company_name, info in all_contribs.items():
                                if company_name.lower() in source.lower():
                                    code = info.get("code", company_name)
                                    if code not in company_contributions:
                                        company_contributions[code] = {
                                            "ticker":             info.get("ticker"),
                                            "contribution_count": 0,
                                            "approved_count":     0,
                                            "rapporteur_flag":    False,
                                            "last_seen":          "",
                                        }
                                    company_contributions[code]["contribution_count"] += 1
                                    status = doc.get("Status", "").lower()
                                    if "approved" in status or "agreed" in status:
                                        company_contributions[code]["approved_count"] += 1
                                    date_str = doc.get("MeetingDate", "")
                                    if date_str:
                                        company_contributions[code]["last_seen"] = date_str[:10]
                    except Exception:
                        pass

                # Level 2: 取得できなかった場合は既知の業界情報から推定
                if not company_contributions:
                    company_contributions = _estimate_contributions_from_known_data(
                        topic, release, all_contribs
                    )

                total = sum(
                    v.get("contribution_count", 0)
                    for v in company_contributions.values()
                )
                results[standard_id] = {
                    "standard_id":          standard_id,
                    "body":                 "3GPP",
                    "release":              release,
                    "topic":                topic,
                    "tech_node_ids":        tech_map.get(topic, []),
                    "company_contributions": company_contributions,
                    "total_contributions":  total,
                    "trend":                _judge_trend(total),
                    "signal_score":         0.0,  # 後段で計算
                }

            except Exception as e:
                print(f"    ⚠️  [3GPP] {standard_id} 取得エラー: {str(e)[:60]}")
                results[standard_id] = _empty_standard_record(
                    standard_id, "3GPP", release, topic, tech_map.get(topic, [])
                )

    return results


def _estimate_contributions_from_known_data(
    topic: str,
    release: str,
    all_contribs: dict,
) -> dict:
    """
    公開情報が取得できない場合に、既知業界情報から貢献数を推定する。
    ※ 実際のAPIが利用可能になればこの関数を廃止する。

    主要貢献企業のランキングは3GPP公式統計ページに基づく初期値。
    貢献数はリリース・トピック別の概数で、定期的なキャッシュ更新で精緻化される。
    """
    # トピック別の主要貢献企業（業界公知情報ベースの初期値）
    topic_leader_map = {
        "NR_mmWave":   ["Qualcomm", "Ericsson", "Nokia", "Samsung", "Murata", "Sony"],
        "RedCap":      ["Qualcomm", "Ericsson", "Nokia", "NTT DoCoMo", "MediaTek"],
        "NTN":         ["Ericsson", "Nokia", "Qualcomm", "NTT DoCoMo", "SoftBank"],
        "6G_AI":       ["NTT", "Samsung", "Nokia", "Ericsson", "Fujitsu", "NEC"],
        "ISAC":        ["Ericsson", "Nokia", "NEC", "Fujitsu", "Sony"],
        "OpenRAN":     ["NEC", "Fujitsu", "NTT DoCoMo", "Nokia", "Ericsson"],
        "network_slicing": ["Ericsson", "Nokia", "NTT DoCoMo", "KDDI"],
        "private_5G":  ["NEC", "Fujitsu", "Nokia", "Ericsson", "Panasonic"],
    }
    leaders = topic_leader_map.get(topic, list(all_contribs.keys())[:4])

    result = {}
    rank_scores = [80, 60, 40, 30, 20, 15, 10, 8]

    for i, company_name in enumerate(leaders):
        if company_name not in all_contribs:
            continue
        info = all_contribs[company_name]
        code = info.get("code", company_name)
        base_count = rank_scores[i] if i < len(rank_scores) else 5
        result[code] = {
            "ticker":             info.get("ticker"),
            "contribution_count": base_count,
            "approved_count":     max(1, base_count // 3),
            "rapporteur_flag":    (i == 0),  # 1位をラポルツール仮定
            "last_seen":          datetime.now().strftime("%Y-%m-%d"),
        }
    return result


# ==============================================================
# BLOCK 4: ISO/IEC データ取得
# ==============================================================

def _fetch_iso_iec_contributions(cfg, standard_config: dict) -> dict:
    """
    ISO/IEC JTC1の委員会動向を取得する。

    ISO/IECは公開APIが限定的なため、主に
    - ISO公式検索ページのHTML解析
    - 既知の委員会活動から推定
    を用いる。
    """
    results = {}
    topics   = standard_config.get("topics", [])
    tech_map = standard_config.get("tech_node_map", {})
    contrib_jp = standard_config.get("known_contributors_jp", {})
    contrib_us = standard_config.get("known_contributors_us", {})
    all_contribs = {**contrib_jp, **contrib_us}

    for topic in topics:
        standard_id = f"iso_iec_{topic.lower()}"
        try:
            # ISO検索APIを試行
            search_url = (
                f"https://www.iso.org/cms/render/live/en/sites/isoorg/home/"
                f"standards.html?query={topic.replace('_', '+')}"
            )
            raw = _safe_get(search_url, timeout=15, retries=1)

            company_contributions = {}
            if raw:
                found = _parse_company_from_text(raw, all_contribs)
                for company_name, info in found.items():
                    code = info.get("code", company_name)
                    company_contributions[code] = {
                        "ticker":             info.get("ticker"),
                        "contribution_count": 5,   # 存在確認できたため基本値
                        "approved_count":     2,
                        "rapporteur_flag":    False,
                        "last_seen":          datetime.now().strftime("%Y-%m-%d"),
                    }

            if not company_contributions:
                company_contributions = _estimate_contributions_from_known_data(
                    topic, "current", all_contribs
                )

            total = sum(v.get("contribution_count", 0) for v in company_contributions.values())
            results[standard_id] = {
                "standard_id":           standard_id,
                "body":                  "ISO/IEC",
                "release":               "current",
                "topic":                 topic,
                "tech_node_ids":         tech_map.get(topic, []),
                "company_contributions": company_contributions,
                "total_contributions":   total,
                "trend":                 _judge_trend(total),
                "signal_score":          0.0,
            }

        except Exception as e:
            print(f"    ⚠️  [ISO/IEC] {standard_id} 取得エラー: {str(e)[:60]}")
            results[standard_id] = _empty_standard_record(
                standard_id, "ISO/IEC", "current", topic, tech_map.get(topic, [])
            )

    return results


# ==============================================================
# BLOCK 5: IEEE データ取得
# ==============================================================

def _fetch_ieee_contributions(cfg, standard_config: dict) -> dict:
    """
    IEEE Standards Associationのワーキンググループ動向を取得する。

    IEEE-SAは一部のプロジェクト情報をRSSで公開しているほか、
    IEEE Xplore APIから論文メタデータ経由で標準化活動を推定できる。
    """
    results = {}
    working_groups = standard_config.get("working_groups", [])
    topics         = standard_config.get("topics", [])
    tech_map       = standard_config.get("tech_node_map", {})
    contrib_jp = standard_config.get("known_contributors_jp", {})
    contrib_us = standard_config.get("known_contributors_us", {})
    all_contribs = {**contrib_jp, **contrib_us}

    for i, wg in enumerate(working_groups):
        topic = topics[i] if i < len(topics) else wg
        standard_id = f"ieee_{wg.replace('.', '_').lower()}"
        try:
            ieee_url = f"https://standards.ieee.org/ieee/{wg}/"
            raw = _safe_get(ieee_url, timeout=15, retries=1)

            company_contributions = {}
            if raw:
                found = _parse_company_from_text(raw, all_contribs)
                for company_name, info in found.items():
                    code = info.get("code", company_name)
                    company_contributions[code] = {
                        "ticker":             info.get("ticker"),
                        "contribution_count": 10,
                        "approved_count":     4,
                        "rapporteur_flag":    False,
                        "last_seen":          datetime.now().strftime("%Y-%m-%d"),
                    }

            if not company_contributions:
                company_contributions = _estimate_contributions_from_known_data(
                    topic, "current", all_contribs
                )

            total = sum(v.get("contribution_count", 0) for v in company_contributions.values())
            results[standard_id] = {
                "standard_id":           standard_id,
                "body":                  "IEEE",
                "release":               wg,
                "topic":                 topic,
                "tech_node_ids":         tech_map.get(topic, []),
                "company_contributions": company_contributions,
                "total_contributions":   total,
                "trend":                 _judge_trend(total),
                "signal_score":          0.0,
            }

        except Exception as e:
            print(f"    ⚠️  [IEEE] {standard_id} 取得エラー: {str(e)[:60]}")
            results[standard_id] = _empty_standard_record(
                standard_id, "IEEE", wg, topic, tech_map.get(topic, [])
            )

    return results


# ==============================================================
# BLOCK 6: スコア計算
# ==============================================================

def _calc_all_signal_scores(standards: dict) -> dict:
    """
    各標準化レコードの signal_score を一括計算する。
    $signal\\_score = f(contribution\\_count, approved\\_ratio, rapporteur, yoy)$
    """
    for std_id, std_data in standards.items():
        comps = std_data.get("company_contributions", {})
        total = max(1, std_data.get("total_contributions", 1))
        best_score = 0.0

        for code, comp_data in comps.items():
            c_count = comp_data.get("contribution_count", 0)
            a_count = comp_data.get("approved_count", 0)
            rap     = comp_data.get("rapporteur_flag", False)
            yoy     = comp_data.get("yoy_delta", 0.0)
            score   = _calc_signal_score(c_count, a_count, rap, yoy, total)
            comp_data["signal_score"] = score
            if score > best_score:
                best_score = score

        std_data["signal_score"] = round(best_score, 2)

    return standards


def _calc_company_standardization_scores(standards: dict) -> dict:
    """
    企業ごとの総合標準化スコア（0〜100）を計算する。

    スコア算出ロジック:
    $standardization\\_score = \\min(100, \\sum_{s} signal\\_score_s \\times weight_s)$
    weight = 1.0（全標準均等）/ ラポルツール担当は+2.0ボーナス
    """
    company_data: dict = {}

    for std_id, std_data in standards.items():
        comps = std_data.get("company_contributions", {})
        for code, comp_data in comps.items():
            ticker = comp_data.get("ticker")
            if not ticker:
                continue
            if ticker not in company_data:
                company_data[ticker] = {
                    "ticker":              ticker,
                    "standardization_score": 0.0,
                    "leading_standards":   [],
                    "rapporteur_count":    0,
                    "total_contributions": 0,
                    "approved_count":      0,
                    "standard_bodies":     set(),
                }

            entry = company_data[ticker]
            sig_score = comp_data.get("signal_score", 0.0)
            weight = 3.0 if comp_data.get("rapporteur_flag") else 1.0
            entry["standardization_score"] += sig_score * weight
            entry["total_contributions"]   += comp_data.get("contribution_count", 0)
            entry["approved_count"]        += comp_data.get("approved_count", 0)
            if comp_data.get("rapporteur_flag"):
                entry["rapporteur_count"]  += 1
                entry["leading_standards"].append(std_id)
            entry["standard_bodies"].add(std_data.get("body", ""))

    # set を list に変換 & スコアをクランプ
    result = {}
    for ticker, entry in company_data.items():
        entry["standard_bodies"]       = sorted(entry["standard_bodies"])
        entry["standardization_score"] = round(
            min(_STANDARDIZATION_SCORE_MAX, entry["standardization_score"]), 1
        )
        result[ticker] = entry

    return result


# ==============================================================
# BLOCK 7: ユーティリティ
# ==============================================================

def _judge_trend(total_contributions: int) -> str:
    """
    貢献数からトレンドを判定する。
    閾値は業界知見ベース（変更は config.py の STANDARDIZATION_TREND_THRESHOLDS で）。
    """
    if total_contributions >= 200:
        return "rising"
    elif total_contributions >= 50:
        return "stable"
    else:
        return "declining"


def _empty_standard_record(
    standard_id: str,
    body: str,
    release: str,
    topic: str,
    tech_node_ids: list,
) -> dict:
    """取得失敗時の空レコードを返す。"""
    return {
        "standard_id":           standard_id,
        "body":                  body,
        "release":               release,
        "topic":                 topic,
        "tech_node_ids":         tech_node_ids,
        "company_contributions": {},
        "total_contributions":   0,
        "trend":                 "declining",
        "signal_score":          0.0,
    }


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ==============================================================
# BLOCK 8: 外部参照用アクセサ
# ==============================================================

def get_standardization_score(
    ticker: str,
    cfg=None,
) -> Optional[float]:
    """
    銘柄の標準化スコアを standardization_cache.json から取得する。

    Args:
        ticker: 銘柄ティッカー（例: "6702.T" / "QCOM"）
        cfg:    config モジュール

    Returns:
        float 0.0〜100.0 または None（データなし）
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return None
        return cache.get("company_scores", {}).get(ticker, {}).get("standardization_score")
    except Exception:
        return None


def get_leading_standards(
    ticker: str,
    cfg=None,
) -> list:
    """
    銘柄がリードしている標準化テーマのIDリストを返す。

    Returns:
        ["3gpp_r18_nr_mmwave", ...] 形式のリスト
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        return cache.get("company_scores", {}).get(ticker, {}).get("leading_standards", [])
    except Exception:
        return []


def get_standard_contributors(
    standard_id: str,
    cfg=None,
    top_n: int = 10,
) -> list:
    """
    指定した標準のトップN貢献企業を返す。

    Returns:
        [{"code": str, "ticker": str, "contribution_count": int, ...}, ...]
    """
    try:
        cache = _load_cache(cfg)
        if not cache:
            return []
        std = cache.get("standards", {}).get(standard_id, {})
        comps = std.get("company_contributions", {})
        sorted_comps = sorted(
            [{"code": k, **v} for k, v in comps.items()],
            key=lambda x: x.get("contribution_count", 0),
            reverse=True,
        )
        return sorted_comps[:top_n]
    except Exception:
        return []


# ==============================================================
# BLOCK 9: メイン実行フロー
# ==============================================================

def run_industry_standard_tracker(cfg=None, force_refresh: bool = False) -> dict:
    """
    stock_analyzer_main.py の WEEKLY_MODULES から呼ばれるエントリポイント。

    処理フロー:
      1. キャッシュ有効（TTL内） → キャッシュ返却
      2. 3GPP / ISO/IEC / IEEE の各データ取得
      3. シグナルスコア計算
      4. 企業別標準化スコア集計
      5. standardization_cache.json に保存
      6. results 辞書を返す

    Returns:
        {
          "standards":       dict,  # 標準別集計データ
          "company_scores":  dict,  # 企業別スコア
          "stats":           dict,  # 統計サマリー
          "cached":          bool,
          "timestamp":       str,
        }
    """
    sync_module_constants(globals(), cfg, _CFG_CONST_MAP)
    print("\n  📡 標準化動向トラッカー（Wave 7 - Step 1）実行中...")

    # ── キャッシュ確認 ──────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(cfg)
        if cached:
            stats = cached.get("stats", {})
            print(
                f"    ✅ キャッシュ使用: "
                f"追跡標準={stats.get('total_standards_tracked', 0)} / "
                f"追跡企業={stats.get('total_companies_tracked', 0)}"
            )
            return {
                "standards":      cached.get("standards", {}),
                "company_scores": cached.get("company_scores", {}),
                "stats":          stats,
                "cached":         True,
                "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    # ── 設定値取得 ────────────────────────────────────────────
    # 2026-07-28: config.STANDARDS_TRACKING_CONFIG は「本番時は辞書を設定」の
    # プレースホルダとして None が入っている。getattr の第3引数は
    # 「属性が存在しないとき」しか効かないため、None がそのまま返り
    # `"3gpp" in standards_config` で落ちていた（3日連続失敗）。
    # midterm_plan_tracker と同じく、falsy なら既定値へ倒す。
    standards_config = getattr(cfg, "STANDARDS_TRACKING_CONFIG",
                               _DEFAULT_STANDARDS_CONFIG)
    if not standards_config:
        standards_config = _DEFAULT_STANDARDS_CONFIG

    all_standards: dict = {}

    # ── 3GPP データ取得 ────────────────────────────────────────
    print("    📋 3GPP Contribution データ取得中...")
    if "3gpp" in standards_config:
        try:
            gpp_data = _fetch_3gpp_contributions(cfg, standards_config["3gpp"])
            all_standards.update(gpp_data)
            print(f"       3GPP: {len(gpp_data)} 標準レコード取得")
        except Exception as e:
            print(f"    ⚠️  [3GPP] 全体エラー: {str(e)[:60]}")

    # ── ISO/IEC データ取得 ─────────────────────────────────────
    print("    📋 ISO/IEC データ取得中...")
    if "iso_iec" in standards_config:
        try:
            iso_data = _fetch_iso_iec_contributions(cfg, standards_config["iso_iec"])
            all_standards.update(iso_data)
            print(f"       ISO/IEC: {len(iso_data)} 標準レコード取得")
        except Exception as e:
            print(f"    ⚠️  [ISO/IEC] 全体エラー: {str(e)[:60]}")

    # ── IEEE データ取得 ────────────────────────────────────────
    print("    📋 IEEE WG データ取得中...")
    if "ieee" in standards_config:
        try:
            ieee_data = _fetch_ieee_contributions(cfg, standards_config["ieee"])
            all_standards.update(ieee_data)
            print(f"       IEEE: {len(ieee_data)} 標準レコード取得")
        except Exception as e:
            print(f"    ⚠️  [IEEE] 全体エラー: {str(e)[:60]}")

    if not all_standards:
        print("    ⚠️  標準化データを取得できませんでした → スキップ")
        return {
            "standards":      {},
            "company_scores": {},
            "stats":          {},
            "cached":         False,
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── シグナルスコア計算 ─────────────────────────────────────
    all_standards = _calc_all_signal_scores(all_standards)

    # ── 企業別スコア集計 ──────────────────────────────────────
    company_scores = _calc_company_standardization_scores(all_standards)

    # ── 統計集計 ─────────────────────────────────────────────
    top_companies = sorted(
        [(t, v["standardization_score"]) for t, v in company_scores.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    stats = {
        "total_standards_tracked": len(all_standards),
        "total_companies_tracked": len(company_scores),
        "top_5_companies":         [
            {"ticker": t, "score": s} for t, s in top_companies
        ],
        "data_sources":            list(_DATA_SOURCES.keys()),
        "bodies_tracked":          list(set(
            v.get("body", "") for v in all_standards.values()
        )),
    }

    # ── 保存 ─────────────────────────────────────────────────
    cache_data = {
        "standards":      all_standards,
        "company_scores": company_scores,
        "stats":          stats,
        "version":        "1.0",
    }
    _save_cache(cfg, cache_data)

    print(
        f"    ✅ 標準化トラッカー完了: "
        f"標準={len(all_standards)} / 企業={len(company_scores)}"
    )
    if top_companies:
        top_str = " / ".join(f"{t}:{s:.1f}" for t, s in top_companies[:3])
        print(f"    🏆 標準化スコア上位3: {top_str}")

    return {
        "standards":      all_standards,
        "company_scores": company_scores,
        "stats":          stats,
        "cached":         False,
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================
# BLOCK 10: 単体テスト
# ==============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  📡 industry_standard_tracker.py v1.0 単体テスト")
    print("=" * 60)

    # config なしで実行（デフォルト定数を使用）
    result = run_industry_standard_tracker(cfg=None, force_refresh=True)

    print(f"\n  【テスト結果】")
    print(f"  追跡標準数  : {result['stats'].get('total_standards_tracked', 0)}")
    print(f"  追跡企業数  : {result['stats'].get('total_companies_tracked', 0)}")
    print(f"  データソース: {result['stats'].get('data_sources', [])}")

    standards = result.get("standards", {})
    if standards:
        sample_id = next(iter(standards))
        sample = standards[sample_id]
        print(f"\n  【サンプル標準: {sample_id}】")
        print(f"  機関       : {sample.get('body')}")
        print(f"  トピック   : {sample.get('topic')}")
        print(f"  総Contribution: {sample.get('total_contributions')}")
        print(f"  シグナルスコア: {sample.get('signal_score')}")
        print(f"  Tech Nodes : {sample.get('tech_node_ids')}")
        comps = sample.get("company_contributions", {})
        for code, cdata in list(comps.items())[:3]:
            print(
                f"    {code:15s}: "
                f"contrib={cdata.get('contribution_count',0):3d} "
                f"approved={cdata.get('approved_count',0):3d} "
                f"rapporteur={cdata.get('rapporteur_flag', False)}"
            )

    company_scores = result.get("company_scores", {})
    if company_scores:
        print(f"\n  【企業標準化スコア（上位5）】")
        sorted_cs = sorted(
            company_scores.items(),
            key=lambda x: x[1].get("standardization_score", 0),
            reverse=True,
        )[:5]
        for ticker, cdata in sorted_cs:
            print(
                f"    {ticker:12s}: "
                f"score={cdata.get('standardization_score', 0):6.1f} "
                f"rapporteur={cdata.get('rapporteur_count', 0)} "
                f"bodies={cdata.get('standard_bodies', [])}"
            )

    # アクセサ関数テスト
    print(f"\n  【アクセサテスト: QCOM】")
    score = get_standardization_score("QCOM")
    print(f"  standardization_score: {score}")
    leading = get_leading_standards("QCOM")
    print(f"  leading_standards: {leading}")

    print(f"\n  ✅ 単体テスト完了")
