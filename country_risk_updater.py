# ==============================================================
# country_risk_updater.py  v1.0  (Wave 6 - G-N2)
# ==============================================================
# 【役割】
#   GDELT / 外務省RSS / 米国国務省RSS を日次でクロールし、
#   geopolitics.country_risk_daily テーブルを MERGE 更新する。
#
# 【データソース詳細】
#
#   ① GDELT Event API（無料・APIキー不要）
#      URL: https://api.gdeltproject.org/api/v2/doc/doc
#      用途: 国別の不安定性スコア（Goldstein Scale 集計）
#      集計ロジック:
#        - 過去3日間のイベントデータから対象国への言及を収集
#        - Goldstein Scale: -10（最大敵対）〜+10（最大協調）
#        - instability_score = max(0, -avg(goldstein)) × 10
#          （マイナス値ほど不安定, 0〜100 に変換）
#
#   ② 外務省 海外安全情報 RSS
#      URL: https://www.anzen.mofa.go.jp/rss/anzen_outer.xml
#      用途: 危険情報レベル（0〜4）の自動抽出
#      レベル定義:
#        0: 危険情報なし
#        1: 十分注意してください
#        2: 不要不急の渡航は止めてください
#        3: 渡航は止めてください（レベル3）
#        4: 退避してください（レベル4）
#
#   ③ 米国国務省 旅行警告 RSS
#      URL: https://travel.state.gov/content/travel/en/traveladvisories/RSS.xml
#      用途: 渡航警告レベル（1〜4）の自動抽出
#      レベル定義:
#        1: Exercise Normal Precautions
#        2: Exercise Increased Caution
#        3: Reconsider Travel
#        4: Do Not Travel
#
# 【合成スコア計算式】
#   $composite\_risk\_score = gdelt \times 0.50 + mofa \times 12.5 + state \times 12.5$
#   （各成分を0〜100スケールに正規化後の加重平均）
#   $gdelt\_contribution = gdelt\_instability \times 0.5$
#   $mofa\_contribution  = (foreign\_ministry\_level / 4) \times 100 \times 0.25$
#   $state\_contribution = (state\_dept\_level / 4) \times 100 \times 0.25$
#
# 【実行方法】
#   stock_analyzer_main.py の run_all_tasks() から日次自動実行
#   または Colab で単体実行:
#     python country_risk_updater.py
#
# 【エラー処理方針】
#   - 各データソースは独立して try/except で保護
#   - 1ソースが失敗しても残り2ソースで composite_risk_score を算出
#   - ネットワーク障害時は前日値をそのまま保持（BQ更新をスキップ）
#
# 【BigQuery 負荷】
#   - 日次1回のみ実行（country_profile_builder とは異なり毎日更新）
#   - 対象国数×1行 の INSERT/MERGE のみ（読み取りなし）
#   - 無料枠の INSERT は月10GB まで → 1日1KB未満なので問題なし
# ==============================================================

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ==============================================================
# BLOCK 1: 定数定義
# ==============================================================

# 外務省RSSのURL
MOFA_RSS_URL = "https://www.anzen.mofa.go.jp/rss/anzen_outer.xml"

# 米国国務省RSSのURL
STATE_DEPT_RSS_URL = (
    "https://travel.state.gov/content/travel/en/traveladvisories/RSS.xml"
)

# GDELT Doc API（国名検索・Themes=UNREST）
# クエリ設計: 国名 AND (UNREST OR MILITARY OR CONFLICT) の件数×Goldsteinスコア
GDELT_API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# HTTPリクエスト共通ヘッダー
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; StockAnalyzer/1.0; "
        "+https://github.com/user/stock-analyzer)"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# RSSフェッチタイムアウト（秒）
_FETCH_TIMEOUT = 20

# リトライ設定
_MAX_RETRIES = 3
_RETRY_WAIT  = 5   # 秒

# GDELT レート制限対策（2026-06-25 本番で全59国が 429 になり、各国3回リトライで
# 約31分浪費した実害への対策）
#   - 1回の実行で GDELT を引く最大国数（優先国から）。cfg.COUNTRY_RISK_GDELT_MAX で上書き可。
#   - 連続失敗がこの数に達したら以降の GDELT 取得は打ち切る（サーキットブレーカー）。
# 2026-07-12調整: 本番ログで30国中大半が429（GitHub共有IPはGDELTに強く制限され、
# 各国リトライ3回×待機で計25分浪費）。①上限30→15 ②累計失敗でも回路を開く
# ③429はリトライしない（_fetch_url側）の3点で「無駄な25分」を数分に短縮。
# 2026-07-17調整: 上限15国に絞った後も全件429で全滅する日が常態化（GitHub共有IP
# はGDELTに恒常的にブロックされている）。①サーキット閾値5→3に短縮し全滅日は
# 数件で見切る ②取得失敗時は永続キャッシュ（gdelt_instability_cache.json）を
# 減衰付きで代用する（BLOCK 5-2 参照）。
_GDELT_MAX_COUNTRIES     = 15
_GDELT_CIRCUIT_THRESHOLD = 3      # 連続失敗の閾値（2026-07-17: 5→3に短縮）
_GDELT_TOTAL_FAIL_LIMIT  = 8      # 累計失敗の閾値（成功が挟まっても打ち切る）

# GDELT「最後に取れた値」永続キャッシュ（取得失敗時の代用に使う）
#   形式: {country_code: {"score": float, "as_of": "YYYY-MM-DD"}}
_GDELT_CACHE_FILENAME    = "gdelt_instability_cache.json"
_GDELT_CACHE_DECAY_RATE  = 0.05   # 1日あたり5%減衰（下限0でクランプ）

# 外務省危険レベルのテキストパターン（RSS title から抽出）
_MOFA_LEVEL_PATTERNS = [
    (4, r"危険情報.*?レベル\s*4|退避してください"),
    (3, r"危険情報.*?レベル\s*3|渡航は止めてください"),
    (2, r"危険情報.*?レベル\s*2|不要不急の渡航は"),
    (1, r"危険情報.*?レベル\s*1|十分注意してください"),
    (1, r"感染症危険情報"),
]

# 国務省危険レベルのテキストパターン（RSS description から抽出）
_STATE_LEVEL_PATTERNS = [
    (4, r"Level\s*4|Do Not Travel"),
    (3, r"Level\s*3|Reconsider Travel"),
    (2, r"Level\s*2|Exercise Increased Caution"),
    (1, r"Level\s*1|Exercise Normal Precautions"),
]

# 外務省RSS の国名→ISO 3166-1 alpha-3 マッピング（主要国）
# RSSのtitleから国名を抽出するための対応表
MOFA_COUNTRY_MAP: dict[str, str] = {
    "サウジアラビア": "SAU",
    "アラブ首長国連邦": "ARE",
    "イラン": "IRN",
    "イラク": "IRQ",
    "クウェート": "KWT",
    "カタール": "QAT",
    "ロシア": "RUS",
    "ノルウェー": "NOR",
    "アメリカ": "USA",
    "カナダ": "CAN",
    "ナイジェリア": "NGA",
    "リビア": "LBY",
    "アルジェリア": "DZA",
    "オーストラリア": "AUS",
    "チリ": "CHL",
    "ペルー": "PER",
    "南アフリカ": "ZAF",
    "コンゴ民主共和国": "COD",
    "インドネシア": "IDN",
    "フィリピン": "PHL",
    "カザフスタン": "KAZ",
    "モンゴル": "MNG",
    "ギニア": "GIN",
    "ボツワナ": "BWA",
    "ブラジル": "BRA",
    "アルゼンチン": "ARG",
    "ウクライナ": "UKR",
    "タイ": "THA",
    "ベトナム": "VNM",
    "インド": "IND",
    "中国": "CHN",
    "日本": "JPN",
    "韓国": "KOR",
    "台湾": "TWN",
    "シンガポール": "SGP",
    "マレーシア": "MYS",
    "エジプト": "EGY",
    "トルコ": "TUR",
    "イスラエル": "ISR",
    "パキスタン": "PAK",
    "ドイツ": "DEU",
    "フランス": "FRA",
    "イギリス": "GBR",
    "オランダ": "NLD",
    "ポーランド": "POL",
    "メキシコ": "MEX",
    "ザンビア": "ZMB",
    "モザンビーク": "MOZ",
    "パプアニューギニア": "PNG",
    "ウズベキスタン": "UZB",
    "ミャンマー": "MMR",
    "カンボジア": "KHM",
    "ラオス": "LAO",
    "バングラデシュ": "BGD",
    "コロンビア": "COL",
    "ベネズエラ": "VEN",
    "エチオピア": "ETH",
    "タンザニア": "TZA",
    "ジンバブエ": "ZWE",
}

# 国務省RSS の英語国名→ISO 3166-1 alpha-3 マッピング
STATE_COUNTRY_MAP: dict[str, str] = {
    "Saudi Arabia": "SAU",
    "United Arab Emirates": "ARE",
    "Iran": "IRN",
    "Iraq": "IRQ",
    "Kuwait": "KWT",
    "Qatar": "QAT",
    "Russia": "RUS",
    "Norway": "NOR",
    "United States": "USA",
    "Canada": "CAN",
    "Nigeria": "NGA",
    "Libya": "LBY",
    "Algeria": "DZA",
    "Australia": "AUS",
    "Chile": "CHL",
    "Peru": "PER",
    "South Africa": "ZAF",
    "Democratic Republic of the Congo": "COD",
    "Indonesia": "IDN",
    "Philippines": "PHL",
    "Kazakhstan": "KAZ",
    "Mongolia": "MNG",
    "Guinea": "GIN",
    "Botswana": "BWA",
    "Brazil": "BRA",
    "Argentina": "ARG",
    "Ukraine": "UKR",
    "Thailand": "THA",
    "Vietnam": "VNM",
    "India": "IND",
    "China": "CHN",
    "Japan": "JPN",
    "Korea, Republic of": "KOR",
    "Korea, South": "KOR",
    "Taiwan": "TWN",
    "Singapore": "SGP",
    "Malaysia": "MYS",
    "Egypt": "EGY",
    "Turkey": "TUR",
    "Israel": "ISR",
    "Pakistan": "PAK",
    "Germany": "DEU",
    "France": "FRA",
    "United Kingdom": "GBR",
    "Netherlands": "NLD",
    "Poland": "POL",
    "Mexico": "MEX",
    "Zambia": "ZMB",
    "Mozambique": "MOZ",
    "Papua New Guinea": "PNG",
    "Uzbekistan": "UZB",
    "Burma": "MMR",
    "Myanmar": "MMR",
    "Cambodia": "KHM",
    "Laos": "LAO",
    "Bangladesh": "BGD",
    "Colombia": "COL",
    "Venezuela": "VEN",
    "Ethiopia": "ETH",
    "Tanzania": "TZA",
    "Zimbabwe": "ZWE",
}

# GDELT クエリ用英語国名マッピング（GDELT API の sourceurl フィルタ用）
GDELT_COUNTRY_QUERY_MAP: dict[str, str] = {
    "SAU": "Saudi Arabia",
    "ARE": "United Arab Emirates",
    "IRN": "Iran",
    "IRQ": "Iraq",
    "KWT": "Kuwait",
    "QAT": "Qatar",
    "RUS": "Russia",
    "NOR": "Norway",
    "USA": "United States",
    "CAN": "Canada",
    "NGA": "Nigeria",
    "LBY": "Libya",
    "DZA": "Algeria",
    "AUS": "Australia",
    "CHL": "Chile",
    "PER": "Peru",
    "ZAF": "South Africa",
    "COD": "Congo",
    "IDN": "Indonesia",
    "PHL": "Philippines",
    "KAZ": "Kazakhstan",
    "MNG": "Mongolia",
    "GIN": "Guinea",
    "BWA": "Botswana",
    "BRA": "Brazil",
    "ARG": "Argentina",
    "UKR": "Ukraine",
    "THA": "Thailand",
    "VNM": "Vietnam",
    "IND": "India",
    "CHN": "China",
    "JPN": "Japan",
    "KOR": "South Korea",
    "TWN": "Taiwan",
    "SGP": "Singapore",
    "MYS": "Malaysia",
    "EGY": "Egypt",
    "TUR": "Turkey",
    "ISR": "Israel",
    "PAK": "Pakistan",
    "DEU": "Germany",
    "FRA": "France",
    "GBR": "United Kingdom",
    "NLD": "Netherlands",
    "POL": "Poland",
    "MEX": "Mexico",
    "ZMB": "Zambia",
    "MOZ": "Mozambique",
    "PNG": "Papua New Guinea",
    "UZB": "Uzbekistan",
    "MMR": "Myanmar",
    "KHM": "Cambodia",
    "LAO": "Laos",
    "BGD": "Bangladesh",
    "COL": "Colombia",
    "VEN": "Venezuela",
    "ETH": "Ethiopia",
    "TZA": "Tanzania",
    "ZWE": "Zimbabwe",
}


# ==============================================================
# BLOCK 2: HTTPユーティリティ
# ==============================================================

def _fetch_url(url: str, timeout: int = _FETCH_TIMEOUT) -> str | None:
    """
    URLを取得してテキスト文字列を返す。
    失敗時は None を返す（例外を上位に伝播しない）。
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = Request(url, headers=_REQUEST_HEADERS)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                # エンコーディング判定（UTF-8優先 → latin-1 フォールバック）
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("latin-1", errors="replace")
        except (HTTPError, URLError) as e:
            # ★ 2026-07-12: 429（レート制限）は待っても同一実行内では回復しない
            #   （GitHub共有IP起因）ためリトライせず即諦める。1国あたり最大
            #   ~75秒→~30秒に短縮し、サーキットブレーカーへ早く到達させる。
            if isinstance(e, HTTPError) and e.code == 429:
                print(f"    ⚠️  fetch失敗 ({url[:60]}...): HTTP 429（リトライなし）")
                return None
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_WAIT * attempt)
            else:
                print(f"    ⚠️  fetch失敗 ({url[:60]}...): {e}")
                return None
        except Exception as e:
            print(f"    ⚠️  fetch例外 ({url[:60]}...): {e}")
            return None
    return None


# ==============================================================
# BLOCK 3: 外務省RSS パーサー
# ==============================================================

def _parse_mofa_rss(xml_text: str) -> dict[str, int]:
    """
    外務省 海外安全情報 RSS を解析し、
    {country_code: level} の辞書を返す。

    解析戦略:
      1. <item> タグを正規表現で抽出
      2. <title> から国名とレベルを判定
      3. MOFA_COUNTRY_MAP で ISO コードに変換

    Returns:
        {country_code: foreign_ministry_level} （0 = 言及なし）
    """
    result: dict[str, int] = {}
    if not xml_text:
        return result

    # <item>...</item> の全ブロックを抽出
    items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)

    for item in items:
        # <title> を抽出
        title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        # CDATA 除去
        title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title)

        # 危険レベルを判定
        level = 0
        for lv, pattern in _MOFA_LEVEL_PATTERNS:
            if re.search(pattern, title):
                level = lv
                break

        if level == 0:
            continue

        # 国名を判定
        matched_code = None
        for country_ja, code in MOFA_COUNTRY_MAP.items():
            if country_ja in title:
                matched_code = code
                break

        if matched_code:
            # 同一国に複数エントリがある場合は高い方を採用
            existing = result.get(matched_code, 0)
            result[matched_code] = max(existing, level)

    return result


# ==============================================================
# BLOCK 4: 国務省RSS パーサー
# ==============================================================

def _parse_state_dept_rss(xml_text: str) -> dict[str, int]:
    """
    米国国務省 旅行警告 RSS を解析し、
    {country_code: level} の辞書を返す。

    Returns:
        {country_code: state_dept_level}
    """
    result: dict[str, int] = {}
    if not xml_text:
        return result

    items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)

    for item in items:
        # <title> から国名を抽出（"Country Name Travel Advisory - Level X" 形式）
        title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        desc_m  = re.search(r"<description>(.*?)</description>", item, re.DOTALL)

        text = ""
        if title_m:
            text += title_m.group(1)
        if desc_m:
            text += " " + desc_m.group(1)
        text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text)

        # レベル判定
        level = 0
        for lv, pattern in _STATE_LEVEL_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                level = lv
                break

        if level == 0:
            continue

        # 国名判定
        matched_code = None
        for country_en, code in STATE_COUNTRY_MAP.items():
            if country_en.lower() in text.lower():
                matched_code = code
                break

        if matched_code:
            existing = result.get(matched_code, 0)
            result[matched_code] = max(existing, level)

    return result


# ==============================================================
# BLOCK 5: GDELT API パーサー
# ==============================================================

def _fetch_gdelt_instability(
    country_code: str,
    days_back: int = 3,
) -> float:
    """
    GDELT Doc API で指定国の過去N日間の不安定性スコアを取得する。

    スコア計算式:
      GDELT の Tone (感情スコア) を用いて不安定性を推計する。
      $instability = max(0, -avg\\_tone) \times 5.0$
      （avg_tone が負値ほど不安定, 0〜100 にクリップ）

    GDELT API パラメータ:
      mode=ArtList: 記事リスト取得
      maxrecords=250: 最大250件
      query: 国名 AND (conflict OR unrest OR sanction OR military)
      timespan: 3d（3日間）
      format=json

    Args:
        country_code: ISO 3166-1 alpha-3
        days_back: 遡る日数（デフォルト3日）

    Returns:
        float: instability_score 0〜100（取得失敗時は -1.0）
    """
    country_en = GDELT_COUNTRY_QUERY_MAP.get(country_code)
    if not country_en:
        return -1.0

    # クエリ組み立て
    # GDELT は URL エンコードされたクエリを受け付ける
    from urllib.parse import quote
    query = f'"{country_en}" (conflict OR unrest OR sanction OR military OR protest)'
    encoded_query = quote(query)
    url = (
        f"{GDELT_API_BASE}"
        f"?query={encoded_query}"
        f"&mode=ArtList"
        f"&maxrecords=250"
        f"&timespan={days_back}d"
        f"&format=json"
    )

    try:
        text = _fetch_url(url, timeout=30)
        if not text:
            return -1.0

        data = json.loads(text)
        articles = data.get("articles", [])

        if not articles:
            # 記事0件 = リスク情報なし → スコア0
            return 0.0

        # Tone を平均（GDELT の tone は -100〜+100 程度）
        tones = []
        for art in articles:
            tone = art.get("tone")
            if tone is not None:
                try:
                    tones.append(float(tone))
                except (TypeError, ValueError):
                    pass

        if not tones:
            return 0.0

        avg_tone = sum(tones) / len(tones)
        # 負値ほど敵対的・不安定 → instability に変換
        # $instability = \max(0, -avg\_tone) \times 5.0$ (0〜100)
        instability = max(0.0, -avg_tone) * 5.0
        return round(min(100.0, instability), 2)

    except Exception as e:
        print(f"    ⚠️  GDELT [{country_code}] 取得エラー: {str(e)[:80]}")
        return -1.0


# ==============================================================
# BLOCK 5-2: GDELT「最後に取れた値」永続キャッシュ（2026-07-17追加）
# ==============================================================
# GitHub Actions の共有IPがGDELTに恒常的にブロックされ「取りに行く」戦略が
# 成立しない日が多い実害への対策。取得成功時に永続キャッシュへ書き込み、
# 取得失敗時（429・レート制限HTML誤200等すべて -1.0 に集約される）はキャッシュ値を
# 経過日数に応じて減衰させて代用する。キャッシュにも無ければ従来どおり -1.0（RSSのみで評価）。

def decayed_score(
    score: float,
    days_old: int,
    rate: float = _GDELT_CACHE_DECAY_RATE,
) -> float:
    """
    キャッシュされたGDELTスコアを経過日数に応じて減衰させる純粋関数。

    $decayed = \\max(0,\\ score \\times \\max(0,\\ 1 - rate \\times days\\_old))$

    Args:
        score:    キャッシュされていた元スコア（0〜100）
        days_old: キャッシュ作成日からの経過日数
        rate:     1日あたりの減衰率（デフォルト5%）

    Returns:
        float: 減衰後スコア（下限0でクランプ）
    """
    if days_old <= 0:
        return round(max(0.0, float(score)), 2)
    factor = max(0.0, 1.0 - rate * days_old)
    return round(max(0.0, float(score) * factor), 2)


def _gdelt_cache_path(cfg) -> Path:
    """GDELTキャッシュファイルのフルパスを返す。"""
    d = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) if cfg else Path("stock_data/cache")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _GDELT_CACHE_FILENAME


def _load_gdelt_cache(cfg) -> dict:
    """GDELTキャッシュをJSONファイルから読み込む。失敗時は空辞書。"""
    try:
        p = _gdelt_cache_path(cfg)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_gdelt_cache(cfg, cache: dict) -> None:
    """GDELTキャッシュをJSONファイルに保存する。"""
    try:
        p = _gdelt_cache_path(cfg)
        p.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  GDELTキャッシュ保存失敗: {str(e)[:60]}")


def _update_gdelt_cache(cache: dict, country_code: str, score: float, today: date) -> None:
    """取得成功時にキャッシュを更新する純粋関数（cacheを破壊的に更新）。"""
    cache[country_code] = {"score": round(float(score), 2), "as_of": today.isoformat()}


def _gdelt_cached_or_fallback(
    cache: dict,
    country_code: str,
    today: date,
) -> tuple[float, int | None]:
    """
    取得失敗時にキャッシュから減衰後スコアを返す純粋関数。

    Returns:
        (score, days_old): キャッシュが無い/不正な場合は (-1.0, None)
    """
    entry = cache.get(country_code)
    if not entry or "score" not in entry or "as_of" not in entry:
        return -1.0, None
    try:
        as_of = date.fromisoformat(entry["as_of"])
    except Exception:
        return -1.0, None
    days_old = max(0, (today - as_of).days)
    score = decayed_score(float(entry["score"]), days_old)
    return score, days_old


# ==============================================================
# BLOCK 6: 合成リスクスコア計算
# ==============================================================

def _calc_composite_risk(
    gdelt_instability: float,
    mofa_level: int,
    state_level: int,
) -> float:
    """
    3ソースを加重平均して composite_risk_score (0〜100) を算出する。

    重み:
      GDELT instability : 50%
      外務省レベル      : 25%
      国務省レベル      : 25%

    各成分のスケール変換:
      $gdelt\\_norm   = gdelt\\_instability$ （既に0〜100）
      $mofa\\_norm    = (mofa\\_level / 4) \times 100$
      $state\\_norm   = (state\\_dept\\_level / 4) \times 100$

    $composite = gdelt\\_norm \times 0.50 + mofa\\_norm \times 0.25 + state\\_norm \times 0.25$

    データ欠損時の処理:
      - GDELT取得失敗（-1.0）: GDELT成分をゼロとして計算し残り50%をスケーリング
      - RSS取得失敗（0）: そのまま0として扱う
    """
    components = []
    weights    = []

    # GDELT
    if gdelt_instability >= 0:
        components.append(float(gdelt_instability))
        weights.append(0.50)
    else:
        # GDELT取得失敗時はRSSの重みを増やす
        components.append(0.0)
        weights.append(0.0)

    # 外務省
    mofa_norm = (mofa_level / 4.0) * 100.0
    components.append(mofa_norm)
    weights.append(0.25)

    # 国務省
    state_norm = (state_level / 4.0) * 100.0
    components.append(state_norm)
    weights.append(0.25)

    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0

    # 正規化して加重平均
    composite = sum(c * w for c, w in zip(components, weights)) / total_weight
    return round(min(100.0, max(0.0, composite)), 2)


# ==============================================================
# BLOCK 7: BQ MERGE クエリ生成
# ==============================================================

def _build_risk_merge_query(
    project_id: str,
    rows: list[dict],
) -> str:
    """
    geopolitics.country_risk_daily への MERGE クエリを生成する。
    (country_code, risk_date) を複合主キーとして UPSERT する。
    """
    if not rows:
        return ""

    value_rows = []
    for r in rows:
        def _s(v):
            if v is None:
                return "NULL"
            return "'" + str(v).replace("'", "\\'") + "'"

        def _f(v):
            if v is None:
                return "NULL"
            return str(float(v))

        def _i(v):
            if v is None:
                return "NULL"
            return str(int(v))

        value_rows.append(
            f"  ({_s(r['country_code'])}, "
            f"DATE('{r['risk_date']}'), "
            f"{_f(r['gdelt_instability'])}, "
            f"{_i(r['foreign_ministry_level'])}, "
            f"{_i(r['state_dept_level'])}, "
            f"{_f(r['composite_risk_score'])}, "
            f"CURRENT_TIMESTAMP())"
        )

    values_sql = ",\n".join(value_rows)

    return f"""
MERGE `{project_id}.geopolitics.country_risk_daily` AS T
USING (
  SELECT * FROM UNNEST(ARRAY<STRUCT<
      country_code STRING,
      risk_date DATE,
      gdelt_instability FLOAT64,
      foreign_ministry_level INT64,
      state_dept_level INT64,
      composite_risk_score FLOAT64,
      updated_at TIMESTAMP
    >>[
{values_sql}
  ])
) AS S
ON T.country_code = S.country_code
   AND T.risk_date = S.risk_date
WHEN MATCHED THEN
  UPDATE SET
    T.gdelt_instability      = S.gdelt_instability,
    T.foreign_ministry_level = S.foreign_ministry_level,
    T.state_dept_level       = S.state_dept_level,
    T.composite_risk_score   = S.composite_risk_score,
    T.updated_at             = S.updated_at
WHEN NOT MATCHED THEN
  INSERT (
    country_code, risk_date,
    gdelt_instability, foreign_ministry_level, state_dept_level,
    composite_risk_score, updated_at
  )
  VALUES (
    S.country_code, S.risk_date,
    S.gdelt_instability, S.foreign_ministry_level, S.state_dept_level,
    S.composite_risk_score, S.updated_at
  )
"""


# ==============================================================
# BLOCK 8: メインエントリポイント
# ==============================================================

def run_country_risk_update(
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
    target_codes: list[str] | None = None,
    skip_gdelt: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    国別リスクスコアを日次更新する。
    stock_analyzer_main.py の run_all_tasks() から呼び出される。

    Args:
        cfg          : config モジュール（PROJECT_ID 等を参照）
        project_id   : GCPプロジェクトID（cfg から取得可能な場合は上書き）
        target_codes : 絞り込み対象 country_code リスト（None = 全件）
        skip_gdelt   : True の場合 GDELT API をスキップ（レート制限回避用）
        dry_run      : True の場合 BQ 実行をスキップ

    Returns:
        {
          "success":        bool,
          "total":          int,    # 処理対象国数
          "updated":        int,    # BQ更新件数
          "skipped":        int,    # データ取得失敗でスキップした国数
          "risk_scores":    dict,   # {country_code: composite_risk_score}
          "mofa_levels":    dict,   # {country_code: foreign_ministry_level}
          "state_levels":   dict,   # {country_code: state_dept_level}
          "gdelt_scores":   dict,   # {country_code: gdelt_instability}
          "high_risk":      list,   # composite_risk_score >= 50 の国リスト
          "error":          str,
          "timestamp":      str,
        }
    """
    print("\n🌐 country_risk_updater: 国別リスク日次更新開始")

    # cfg から project_id を取得（存在する場合）
    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    today_str = date.today().isoformat()

    # 対象国リストを決定
    if target_codes:
        all_codes = target_codes
    else:
        # country_profile_builder から取得
        try:
            from country_profile_builder import get_all_country_codes
            all_codes = get_all_country_codes()
        except ImportError:
            # フォールバック: GDELT_COUNTRY_QUERY_MAP のキーを使用
            all_codes = list(GDELT_COUNTRY_QUERY_MAP.keys())

    print(f"   対象国数: {len(all_codes)} 件")
    print(f"   対象日付: {today_str}")

    # ── ① 外務省RSS 取得・解析 ────────────────────────────────
    mofa_levels: dict[str, int] = {}
    print("   📡 外務省RSS 取得中...")
    try:
        mofa_xml = _fetch_url(MOFA_RSS_URL)
        if mofa_xml:
            mofa_levels = _parse_mofa_rss(mofa_xml)
            print(f"   ✅ 外務省RSS: {len(mofa_levels)} 件の危険情報取得")
            # 高レベル国を表示
            high_mofa = {k: v for k, v in mofa_levels.items() if v >= 3}
            if high_mofa:
                print(f"   ⚠️  レベル3以上: {high_mofa}")
        else:
            print("   ⚠️  外務省RSS: 取得失敗（スキップ）")
    except Exception as e:
        print(f"   ⚠️  外務省RSS 解析エラー: {str(e)[:80]}")

    # ── ② 国務省RSS 取得・解析 ─────────────────────────────────
    state_levels: dict[str, int] = {}
    print("   📡 国務省RSS 取得中...")
    try:
        state_xml = _fetch_url(STATE_DEPT_RSS_URL)
        if state_xml:
            state_levels = _parse_state_dept_rss(state_xml)
            print(f"   ✅ 国務省RSS: {len(state_levels)} 件の警告情報取得")
            high_state = {k: v for k, v in state_levels.items() if v >= 3}
            if high_state:
                print(f"   ⚠️  Level3以上: {high_state}")
        else:
            print("   ⚠️  国務省RSS: 取得失敗（スキップ）")
    except Exception as e:
        print(f"   ⚠️  国務省RSS 解析エラー: {str(e)[:80]}")

    # ── ③ GDELT スコア収集 ─────────────────────────────────────
    gdelt_scores: dict[str, float] = {}
    if skip_gdelt:
        print("   ⏩ GDELT: スキップ（skip_gdelt=True）")
    else:
        # GDELT は高リスク国（外務省/国務省でLv2以上）を優先して取得。
        # API負荷・レート制限(429)対策として、取得は上限件数までに絞り、
        # 連続失敗が閾値に達したら以降は打ち切る（旧コードは全59件を無条件で
        # 順次取得し、全件429時に各国3回リトライで約31分浪費していた）。
        priority_codes = list(set(
            list(mofa_levels.keys()) + list(state_levels.keys())
        ))
        remaining = [c for c in all_codes if c not in priority_codes]

        # 優先→残り の順に並べ、上限件数まで取得（残りは RSS のみで評価）
        ordered_codes = priority_codes + remaining
        _gdelt_limit  = int(getattr(cfg, "COUNTRY_RISK_GDELT_MAX",
                                    _GDELT_MAX_COUNTRIES)) if cfg else _GDELT_MAX_COUNTRIES
        ordered_codes = ordered_codes[:max(0, _gdelt_limit)]
        print(f"   📡 GDELT 取得中（{len(ordered_codes)} 件・順次取得 / 上限{_gdelt_limit}）...")

        _consec_fail  = 0
        _total_fail   = 0
        _circuit_open = False
        _gdelt_cache  = _load_gdelt_cache(cfg)
        _cache_hits   = 0
        _today        = date.today()
        for i, code in enumerate(ordered_codes):
            if code not in GDELT_COUNTRY_QUERY_MAP:
                gdelt_scores[code] = -1.0
                continue
            if not _circuit_open:
                score = _fetch_gdelt_instability(code, days_back=3)
                gdelt_scores[code] = score
                # 失敗の監視（-1.0 = 取得失敗。429・レート制限ページの200返却＝JSON
                # パース失敗もすべてここに集約される）。レート制限なら早期に打ち切る。
                # ★ 2026-07-12: 成功が間に挟まると連続カウンタがリセットされ、429の
                #   波状失敗でも回路が開かず25分浪費した実害 → 累計カウンタを併設。
                if score < 0:
                    _consec_fail += 1
                    _total_fail  += 1
                    if (_consec_fail >= _GDELT_CIRCUIT_THRESHOLD
                            or _total_fail >= _GDELT_TOTAL_FAIL_LIMIT):
                        _circuit_open = True
                        print(f"   ⏹️  GDELT: 失敗 連続{_consec_fail}/累計{_total_fail}件"
                              f"（レート制限の可能性）→ 残りはスキップ（RSSのみで評価）")
                else:
                    _consec_fail = 0
                    _update_gdelt_cache(_gdelt_cache, code, score, _today)
                # API負荷軽減のため1件ごとに0.5秒待機
                time.sleep(0.5)
                if (i + 1) % 10 == 0:
                    print(f"   GDELT進捗: {i+1}/{len(ordered_codes)}")
            else:
                gdelt_scores[code] = -1.0

            # ── 取得失敗時（-1.0）は永続キャッシュを減衰付きで代用 ──────
            # 2026-07-17: GitHub共有IPがGDELTに恒常的にブロックされ「取りに行く」
            # 戦略自体が成立しない日が多いための対策。
            if gdelt_scores[code] < 0:
                fb_score, fb_days = _gdelt_cached_or_fallback(_gdelt_cache, code, _today)
                if fb_score >= 0:
                    gdelt_scores[code] = fb_score
                    _cache_hits += 1
                    print(f"   ♻️  GDELT[{code}] キャッシュ代用"
                          f"（{fb_days}日前・減衰後{fb_score:.1f}）")

        _save_gdelt_cache(cfg, _gdelt_cache)
        valid_gdelt = {k: v for k, v in gdelt_scores.items() if v >= 0}
        print(f"   ✅ GDELT: {len(valid_gdelt)} 件取得完了"
              f"（うちキャッシュ代用 {_cache_hits} 件）")

    # ── ④ 合成スコア計算・MERGE行データ構築 ──────────────────────
    rows: list[dict] = []
    risk_scores: dict[str, float] = {}
    skipped_count = 0

    for code in all_codes:
        gdelt_v = gdelt_scores.get(code, -1.0) if not skip_gdelt else -1.0
        mofa_v  = mofa_levels.get(code, 0)
        state_v = state_levels.get(code, 0)

        composite = _calc_composite_risk(gdelt_v, mofa_v, state_v)
        risk_scores[code] = composite

        row = {
            "country_code":          code,
            "risk_date":             today_str,
            "gdelt_instability":     max(0.0, gdelt_v),   # -1.0 は 0.0 として格納
            "foreign_ministry_level": mofa_v,
            "state_dept_level":      state_v,
            "composite_risk_score":  composite,
        }
        rows.append(row)

    # 高リスク国（composite >= 50）
    high_risk = sorted(
        [c for c, s in risk_scores.items() if s >= 50.0],
        key=lambda c: -risk_scores[c],
    )
    if high_risk:
        print(f"   🔴 高リスク国（スコア50以上）: {high_risk}")
        for c in high_risk[:5]:
            print(f"      {c}: composite={risk_scores[c]:.1f} "
                  f"mofa={mofa_levels.get(c,0)} "
                  f"state={state_levels.get(c,0)} "
                  f"gdelt={gdelt_scores.get(c,-1.0):.1f}")

    if dry_run:
        print(f"   [DRY RUN] BQ実行をスキップ（{len(rows)}件）")
        return {
            "success":      True,
            "total":        len(all_codes),
            "updated":      0,
            "skipped":      skipped_count,
            "risk_scores":  risk_scores,
            "mofa_levels":  mofa_levels,
            "state_levels": state_levels,
            "gdelt_scores": gdelt_scores,
            "high_risk":    high_risk,
            "error":        "",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    # ── ⑤ BQ MERGE 実行 ──────────────────────────────────────
    try:
        # BQ クライアント取得
        try:
            import importlib, sys as _sys
            from pathlib import Path as _Path
            for p in ["/content", str(_Path(__file__).parent)]:
                if p not in _sys.path:
                    _sys.path.insert(0, p)
            _bq_mod = importlib.import_module("edinet_bq_client")
            _bq_client = _bq_mod.EDINETBQClient()
            raw_bq = _bq_client.client
        except Exception:
            # フォールバック: google.cloud.bigquery 直接使用
            from google.cloud import bigquery as _bq
            from google.colab import auth as _auth
            _auth.authenticate_user()
            raw_bq = _bq.Client(
                project=_pid,
                location="asia-northeast1",
            )

        merge_sql = _build_risk_merge_query(_pid, rows)
        job = raw_bq.query(merge_sql)
        job.result()

        print(f"   ✅ BQ MERGE 完了: {len(rows)} 件 (JOB: {job.job_id})")
        return {
            "success":      True,
            "total":        len(all_codes),
            "updated":      len(rows),
            "skipped":      skipped_count,
            "risk_scores":  risk_scores,
            "mofa_levels":  mofa_levels,
            "state_levels": state_levels,
            "gdelt_scores": gdelt_scores,
            "high_risk":    high_risk,
            "error":        "",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        err = str(e)
        print(f"   ❌ BQ MERGE 失敗: {err[:200]}")
        return {
            "success":      False,
            "total":        len(all_codes),
            "updated":      0,
            "skipped":      skipped_count,
            "risk_scores":  risk_scores,
            "mofa_levels":  mofa_levels,
            "state_levels": state_levels,
            "gdelt_scores": gdelt_scores,
            "high_risk":    high_risk,
            "error":        err[:500],
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }


# ==============================================================
# BLOCK 9: 他モジュール向け参照ユーティリティ
# ==============================================================

def get_latest_risk_score(
    country_code: str,
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
) -> dict:
    """
    指定国の最新 composite_risk_score を BQ から取得する。
    geo_signal.py 等から呼ばれる参照用関数。

    Args:
        country_code: ISO 3166-1 alpha-3
        cfg: config モジュール（PROJECT_ID 参照用）
        project_id: GCPプロジェクトID

    Returns:
        {
          "country_code":          str,
          "risk_date":             str,
          "gdelt_instability":     float,
          "foreign_ministry_level": int,
          "state_dept_level":      int,
          "composite_risk_score":  float,
        }
        取得失敗時は composite_risk_score=0.0 のデフォルト値を返す
    """
    _default = {
        "country_code":           country_code,
        "risk_date":              date.today().isoformat(),
        "gdelt_instability":      0.0,
        "foreign_ministry_level": 0,
        "state_dept_level":       0,
        "composite_risk_score":   0.0,
    }

    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id

    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        _bq     = _bq_mod.EDINETBQClient()
        raw_bq  = _bq.client

        query = f"""
        SELECT
          country_code,
          FORMAT_DATE('%Y-%m-%d', risk_date) AS risk_date,
          gdelt_instability,
          foreign_ministry_level,
          state_dept_level,
          composite_risk_score
        FROM `{_pid}.geopolitics.country_risk_daily`
        WHERE country_code = '{country_code}'
        ORDER BY risk_date DESC
        LIMIT 1
        """
        rows = list(raw_bq.query(query).result())
        if rows:
            row = rows[0]
            return {
                "country_code":           row.country_code,
                "risk_date":              row.risk_date,
                "gdelt_instability":      float(row.gdelt_instability or 0.0),
                "foreign_ministry_level": int(row.foreign_ministry_level or 0),
                "state_dept_level":       int(row.state_dept_level or 0),
                "composite_risk_score":   float(row.composite_risk_score or 0.0),
            }
        return _default

    except Exception as e:
        print(f"  ⚠️  get_latest_risk_score [{country_code}]: {str(e)[:80]}")
        return _default


def get_risk_scores_batch(
    country_codes: list[str],
    cfg=None,
    project_id: str = "stock-analyzer-edinet",
) -> dict[str, float]:
    """
    複数国の最新 composite_risk_score を一括取得する。
    geo_signal.py 等からバッチ参照する際に使用。

    Args:
        country_codes: ISO 3166-1 alpha-3 のリスト
        cfg: config モジュール
        project_id: GCPプロジェクトID

    Returns:
        {country_code: composite_risk_score}
        取得失敗した国は 0.0 を返す
    """
    result = {c: 0.0 for c in country_codes}

    if not country_codes:
        return result

    _pid = getattr(cfg, "GEO_BQ_PROJECT_ID", project_id) if cfg else project_id
    codes_str = ", ".join(f"'{c}'" for c in country_codes)

    try:
        import importlib, sys as _sys
        from pathlib import Path as _Path
        for p in ["/content", str(_Path(__file__).parent)]:
            if p not in _sys.path:
                _sys.path.insert(0, p)
        _bq_mod = importlib.import_module("edinet_bq_client")
        _bq     = _bq_mod.EDINETBQClient()
        raw_bq  = _bq.client

        query = f"""
        SELECT
          country_code,
          composite_risk_score
        FROM (
          SELECT
            country_code,
            composite_risk_score,
            ROW_NUMBER() OVER (
              PARTITION BY country_code
              ORDER BY risk_date DESC
            ) AS rn
          FROM `{_pid}.geopolitics.country_risk_daily`
          WHERE country_code IN ({codes_str})
        )
        WHERE rn = 1
        """
        for row in raw_bq.query(query).result():
            result[row.country_code] = float(row.composite_risk_score or 0.0)

    except Exception as e:
        print(f"  ⚠️  get_risk_scores_batch: {str(e)[:80]}")

    return result


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    dry_run_flag  = "--dry-run" in sys.argv
    skip_gdelt    = "--skip-gdelt" in sys.argv
    target_flag   = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            target_flag = sys.argv[i + 1].split(",")

    print("=" * 60)
    print("country_risk_updater.py v1.0")
    print("=" * 60)

    result = run_country_risk_update(
        cfg          = None,
        project_id   = "stock-analyzer-edinet",
        target_codes = target_flag,
        skip_gdelt   = skip_gdelt,
        dry_run      = dry_run_flag,
    )

    print(f"\n{'✅ 完了' if result['success'] else '❌ 失敗'}")
    print(f"   処理件数: {result['total']}")
    print(f"   更新件数: {result['updated']}")
    print(f"   高リスク: {result['high_risk']}")
    if result["error"]:
        print(f"   エラー: {result['error']}")
    print(f"   実行時刻: {result['timestamp']}")