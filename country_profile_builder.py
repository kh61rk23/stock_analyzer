# ==============================================================
# country_profile_builder.py  v1.0  (Wave 6 - G-N1)
# ==============================================================
# 【役割】
#   geopolitics.country_profile テーブルに対して
#   国別プロファイルの静的マスターデータを構築・INSERT する。
#   年次更新を想定した「使い捨て可能な完全MERGE型」実装。
#
# 【データソース】
#   - 民主主義指数:     EIU Democracy Index（ハードコード静的値, 年次更新）
#   - 腐敗認識指数:     Transparency International CPI（静的値）
#   - 法の支配指数:     World Justice Project（静的値）
#   - GDP:             World Bank（静的値, USD bn）
#   - 輸出集中度(HHI):  静的値（資源依存国は高値）
#   - 主要輸出品:       静的JSON
#   - 海上ルート隣接:   静的JSON
#   - 軍事同盟:         静的JSON
#   - 制裁フラグ:       OFAC/EU制裁リスト（静的判定）
#   - 日本との貿易依存度: 財務省貿易統計ベース（静的値）
#
# 【対象国】
#   Wave 6 Phase G の対象とする80カ国（主要資源国・地政学関与国）
#   - エネルギー資源国: SAU/ARE/IRN/IRQ/KWT/QAT/RUS/NOR/CAN/USA/BRA/NGA/LBY/DZA
#   - 鉱物資源国:      AUS/CHL/PER/ZAF/COD/PNG/IDN/PHL/KAZ/UZB/MNG/GIN/BWA
#   - 農業輸出国:      BRA/ARG/USA/CAN/AUS/UKR/THA/VNM/IND
#   - 地政学的要衝国:  CHN/JPN/KOR/TWN/SGP/MYS/EGY/TUR/ISR/SAU/IND/PAK
#   - 欧州主要国:      DEU/FRA/GBR/ITA/ESP/POL/NLD/BEL/SWE/NOR/FIN/CHE
#   - その他:         MEX/COL/VEN/ZWE/ETH/TZA/MOZ/ZMB/MMR/KHM/LAO/BGD
#
# 【実行方法】
#   Google Colab で単体実行（初回 or 年次更新時）
#   python country_profile_builder.py
#
# 【BQ接続】
#   edinet_bq_client.EDINETBQClient 経由でMERGE実行
#   テーブルは作成済み前提（edinet_bq_schema_new_20260601.py で作成済み）
#
# 【数式メモ】
#   $HHI = \sum_{i} s_i^2$ （輸出品目シェアの2乗和, 0〜1）
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# ==============================================================
# BLOCK 1: 国別プロファイル静的マスターデータ
# ==============================================================
# 各エントリの構造:
#   country_code        : ISO 3166-1 alpha-3
#   country_name_ja     : 日本語国名
#   region              : 地域区分
#   democracy_index     : EIU民主主義指数 0〜10（高いほど民主的）
#   corruption_index    : TI腐敗認識指数 0〜100（高いほど清廉）
#   rule_of_law         : WJP法の支配指数 0〜1（高いほど法治）
#   gdp_usd_bn          : GDP（十億USD）
#   export_concentration: 輸出集中度HHI 0〜1（高いほど特定品目依存）
#   top_export_commodities: 主要輸出品コードリスト
#   is_landlocked       : 内陸国フラグ
#   sealane_adjacency   : 主要シーレーン隣接リスト
#   military_alliances  : 軍事同盟コードリスト
#   sanction_flag       : OFAC/EU制裁対象フラグ
#   trade_dependency_jp : 日本との貿易依存度 0〜1

COUNTRY_PROFILES: list[dict[str, Any]] = [

    # ── エネルギー資源国 ─────────────────────────────────────

    {
        "country_code": "SAU",
        "country_name_ja": "サウジアラビア",
        "region": "中東",
        "democracy_index": 1.08,
        "corruption_index": 53.0,
        "rule_of_law": 0.54,
        "gdp_usd_bn": 1069.0,
        "export_concentration": 0.72,
        "top_export_commodities": ["crude_oil", "petroleum_products", "petrochemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_hormuz", "red_sea", "bab_el_mandeb"],
        "military_alliances": ["GCC"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.058,
    },
    {
        "country_code": "ARE",
        "country_name_ja": "アラブ首長国連邦",
        "region": "中東",
        "democracy_index": 2.72,
        "corruption_index": 68.0,
        "rule_of_law": 0.64,
        "gdp_usd_bn": 509.0,
        "export_concentration": 0.45,
        "top_export_commodities": ["crude_oil", "petroleum_products", "gold", "aluminum"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_hormuz", "persian_gulf"],
        "military_alliances": ["GCC"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.029,
    },
    {
        "country_code": "IRN",
        "country_name_ja": "イラン",
        "region": "中東",
        "democracy_index": 2.20,
        "corruption_index": 24.0,
        "rule_of_law": 0.32,
        "gdp_usd_bn": 367.0,
        "export_concentration": 0.68,
        "top_export_commodities": ["crude_oil", "natural_gas", "petrochemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_hormuz", "caspian_sea"],
        "military_alliances": [],
        "sanction_flag": True,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "IRQ",
        "country_name_ja": "イラク",
        "region": "中東",
        "democracy_index": 3.43,
        "corruption_index": 20.0,
        "rule_of_law": 0.30,
        "gdp_usd_bn": 268.0,
        "export_concentration": 0.85,
        "top_export_commodities": ["crude_oil"],
        "is_landlocked": False,
        "sealane_adjacency": ["persian_gulf", "strait_of_hormuz"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.022,
    },
    {
        "country_code": "KWT",
        "country_name_ja": "クウェート",
        "region": "中東",
        "democracy_index": 3.78,
        "corruption_index": 46.0,
        "rule_of_law": 0.50,
        "gdp_usd_bn": 161.0,
        "export_concentration": 0.80,
        "top_export_commodities": ["crude_oil", "petroleum_products"],
        "is_landlocked": False,
        "sealane_adjacency": ["persian_gulf", "strait_of_hormuz"],
        "military_alliances": ["GCC"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.015,
    },
    {
        "country_code": "QAT",
        "country_name_ja": "カタール",
        "region": "中東",
        "democracy_index": 3.19,
        "corruption_index": 58.0,
        "rule_of_law": 0.61,
        "gdp_usd_bn": 222.0,
        "export_concentration": 0.78,
        "top_export_commodities": ["lng", "crude_oil", "petrochemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["persian_gulf", "strait_of_hormuz"],
        "military_alliances": ["GCC"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.031,
    },
    {
        "country_code": "RUS",
        "country_name_ja": "ロシア",
        "region": "欧州・ロシア",
        "democracy_index": 2.22,
        "corruption_index": 26.0,
        "rule_of_law": 0.32,
        "gdp_usd_bn": 1863.0,
        "export_concentration": 0.52,
        "top_export_commodities": ["crude_oil", "natural_gas", "coal", "steel", "wheat", "aluminum"],
        "is_landlocked": False,
        "sealane_adjacency": ["arctic_route", "black_sea", "baltic_sea"],
        "military_alliances": ["CSTO"],
        "sanction_flag": True,
        "trade_dependency_jp": 0.008,
    },
    {
        "country_code": "NOR",
        "country_name_ja": "ノルウェー",
        "region": "欧州",
        "democracy_index": 9.81,
        "corruption_index": 84.0,
        "rule_of_law": 0.90,
        "gdp_usd_bn": 579.0,
        "export_concentration": 0.58,
        "top_export_commodities": ["crude_oil", "natural_gas", "fish", "metals"],
        "is_landlocked": False,
        "sealane_adjacency": ["north_sea", "norwegian_sea"],
        "military_alliances": ["NATO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.003,
    },
    {
        "country_code": "USA",
        "country_name_ja": "アメリカ合衆国",
        "region": "北米",
        "democracy_index": 7.85,
        "corruption_index": 69.0,
        "rule_of_law": 0.79,
        "gdp_usd_bn": 27360.0,
        "export_concentration": 0.12,
        "top_export_commodities": ["machinery", "aircraft", "semiconductors", "lng", "crude_oil", "soybeans", "corn"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "atlantic_ocean", "gulf_of_mexico"],
        "military_alliances": ["NATO", "QUAD", "AUKUS"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.152,
    },
    {
        "country_code": "CAN",
        "country_name_ja": "カナダ",
        "region": "北米",
        "democracy_index": 8.88,
        "corruption_index": 76.0,
        "rule_of_law": 0.84,
        "gdp_usd_bn": 2140.0,
        "export_concentration": 0.28,
        "top_export_commodities": ["crude_oil", "natural_gas", "canola", "lumber", "gold", "potash"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "atlantic_ocean", "arctic_route"],
        "military_alliances": ["NATO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.018,
    },
    {
        "country_code": "NGA",
        "country_name_ja": "ナイジェリア",
        "region": "アフリカ",
        "democracy_index": 4.23,
        "corruption_index": 25.0,
        "rule_of_law": 0.38,
        "gdp_usd_bn": 477.0,
        "export_concentration": 0.76,
        "top_export_commodities": ["crude_oil", "natural_gas", "cocoa", "rubber"],
        "is_landlocked": False,
        "sealane_adjacency": ["gulf_of_guinea", "atlantic_ocean"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.005,
    },
    {
        "country_code": "LBY",
        "country_name_ja": "リビア",
        "region": "アフリカ",
        "democracy_index": 2.56,
        "corruption_index": 18.0,
        "rule_of_law": 0.22,
        "gdp_usd_bn": 38.0,
        "export_concentration": 0.92,
        "top_export_commodities": ["crude_oil"],
        "is_landlocked": False,
        "sealane_adjacency": ["mediterranean_sea"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "DZA",
        "country_name_ja": "アルジェリア",
        "region": "アフリカ",
        "democracy_index": 3.66,
        "corruption_index": 36.0,
        "rule_of_law": 0.40,
        "gdp_usd_bn": 239.0,
        "export_concentration": 0.82,
        "top_export_commodities": ["crude_oil", "natural_gas", "petroleum_products"],
        "is_landlocked": False,
        "sealane_adjacency": ["mediterranean_sea"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },

    # ── 鉱物資源国 ─────────────────────────────────────────────

    {
        "country_code": "AUS",
        "country_name_ja": "オーストラリア",
        "region": "オセアニア",
        "democracy_index": 8.86,
        "corruption_index": 75.0,
        "rule_of_law": 0.86,
        "gdp_usd_bn": 1723.0,
        "export_concentration": 0.35,
        "top_export_commodities": ["iron_ore", "coal", "lng", "gold", "copper", "aluminum"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "indian_ocean", "south_china_sea"],
        "military_alliances": ["QUAD", "AUKUS", "ANZUS"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.062,
    },
    {
        "country_code": "CHL",
        "country_name_ja": "チリ",
        "region": "中南米",
        "democracy_index": 7.94,
        "corruption_index": 67.0,
        "rule_of_law": 0.66,
        "gdp_usd_bn": 344.0,
        "export_concentration": 0.48,
        "top_export_commodities": ["copper", "lithium", "molybdenum", "fish", "wine"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "cape_horn"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.009,
    },
    {
        "country_code": "PER",
        "country_name_ja": "ペルー",
        "region": "中南米",
        "democracy_index": 6.23,
        "corruption_index": 36.0,
        "rule_of_law": 0.48,
        "gdp_usd_bn": 268.0,
        "export_concentration": 0.42,
        "top_export_commodities": ["copper", "gold", "zinc", "silver", "fish", "coffee"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.005,
    },
    {
        "country_code": "ZAF",
        "country_name_ja": "南アフリカ",
        "region": "アフリカ",
        "democracy_index": 7.05,
        "corruption_index": 41.0,
        "rule_of_law": 0.52,
        "gdp_usd_bn": 373.0,
        "export_concentration": 0.38,
        "top_export_commodities": ["platinum", "gold", "coal", "iron_ore", "chromium", "manganese"],
        "is_landlocked": False,
        "sealane_adjacency": ["cape_of_good_hope", "indian_ocean", "atlantic_ocean"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.007,
    },
    {
        "country_code": "COD",
        "country_name_ja": "コンゴ民主共和国",
        "region": "アフリカ",
        "democracy_index": 3.14,
        "corruption_index": 20.0,
        "rule_of_law": 0.24,
        "gdp_usd_bn": 67.0,
        "export_concentration": 0.65,
        "top_export_commodities": ["cobalt", "copper", "coltan", "gold", "diamonds"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },
    {
        "country_code": "IDN",
        "country_name_ja": "インドネシア",
        "region": "東南アジア",
        "democracy_index": 6.71,
        "corruption_index": 34.0,
        "rule_of_law": 0.52,
        "gdp_usd_bn": 1371.0,
        "export_concentration": 0.30,
        "top_export_commodities": ["coal", "palm_oil", "nickel", "copper", "lng"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_malacca", "lombok_strait", "sunda_strait", "south_china_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.035,
    },
    {
        "country_code": "PHL",
        "country_name_ja": "フィリピン",
        "region": "東南アジア",
        "democracy_index": 6.73,
        "corruption_index": 34.0,
        "rule_of_law": 0.52,
        "gdp_usd_bn": 437.0,
        "export_concentration": 0.28,
        "top_export_commodities": ["semiconductors", "electronics", "nickel", "copper", "coconut_oil"],
        "is_landlocked": False,
        "sealane_adjacency": ["south_china_sea", "philippine_sea", "celebes_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.021,
    },
    {
        "country_code": "KAZ",
        "country_name_ja": "カザフスタン",
        "region": "中央アジア",
        "democracy_index": 3.14,
        "corruption_index": 36.0,
        "rule_of_law": 0.42,
        "gdp_usd_bn": 261.0,
        "export_concentration": 0.60,
        "top_export_commodities": ["crude_oil", "uranium", "copper", "zinc", "wheat"],
        "is_landlocked": True,
        "sealane_adjacency": ["caspian_sea"],
        "military_alliances": ["CSTO", "SCO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.003,
    },
    {
        "country_code": "MNG",
        "country_name_ja": "モンゴル",
        "region": "東アジア",
        "democracy_index": 6.46,
        "corruption_index": 35.0,
        "rule_of_law": 0.49,
        "gdp_usd_bn": 20.0,
        "export_concentration": 0.72,
        "top_export_commodities": ["coal", "copper", "gold", "iron_ore"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },
    {
        "country_code": "GIN",
        "country_name_ja": "ギニア",
        "region": "アフリカ",
        "democracy_index": 4.21,
        "corruption_index": 27.0,
        "rule_of_law": 0.32,
        "gdp_usd_bn": 24.0,
        "export_concentration": 0.78,
        "top_export_commodities": ["bauxite", "gold", "diamonds", "aluminum"],
        "is_landlocked": False,
        "sealane_adjacency": ["atlantic_ocean"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },
    {
        "country_code": "BWA",
        "country_name_ja": "ボツワナ",
        "region": "アフリカ",
        "democracy_index": 7.61,
        "corruption_index": 59.0,
        "rule_of_law": 0.65,
        "gdp_usd_bn": 23.0,
        "export_concentration": 0.82,
        "top_export_commodities": ["diamonds", "copper", "nickel", "beef"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },

    # ── 農業輸出国 ─────────────────────────────────────────────

    {
        "country_code": "BRA",
        "country_name_ja": "ブラジル",
        "region": "中南米",
        "democracy_index": 6.97,
        "corruption_index": 36.0,
        "rule_of_law": 0.51,
        "gdp_usd_bn": 2174.0,
        "export_concentration": 0.22,
        "top_export_commodities": ["soybeans", "iron_ore", "crude_oil", "sugar", "corn", "beef", "cellulose"],
        "is_landlocked": False,
        "sealane_adjacency": ["atlantic_ocean"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.015,
    },
    {
        "country_code": "ARG",
        "country_name_ja": "アルゼンチン",
        "region": "中南米",
        "democracy_index": 7.02,
        "corruption_index": 38.0,
        "rule_of_law": 0.48,
        "gdp_usd_bn": 641.0,
        "export_concentration": 0.28,
        "top_export_commodities": ["soybeans", "corn", "wheat", "beef", "lithium"],
        "is_landlocked": False,
        "sealane_adjacency": ["atlantic_ocean", "drake_passage"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.004,
    },
    {
        "country_code": "UKR",
        "country_name_ja": "ウクライナ",
        "region": "欧州",
        "democracy_index": 5.42,
        "corruption_index": 36.0,
        "rule_of_law": 0.48,
        "gdp_usd_bn": 174.0,
        "export_concentration": 0.35,
        "top_export_commodities": ["wheat", "corn", "sunflower_oil", "iron_ore", "steel"],
        "is_landlocked": False,
        "sealane_adjacency": ["black_sea"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },
    {
        "country_code": "THA",
        "country_name_ja": "タイ",
        "region": "東南アジア",
        "democracy_index": 6.04,
        "corruption_index": 35.0,
        "rule_of_law": 0.54,
        "gdp_usd_bn": 543.0,
        "export_concentration": 0.18,
        "top_export_commodities": ["electronics", "automobiles", "rice", "rubber", "chemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["south_china_sea", "gulf_of_thailand", "strait_of_malacca"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.031,
    },
    {
        "country_code": "VNM",
        "country_name_ja": "ベトナム",
        "region": "東南アジア",
        "democracy_index": 2.94,
        "corruption_index": 41.0,
        "rule_of_law": 0.48,
        "gdp_usd_bn": 429.0,
        "export_concentration": 0.20,
        "top_export_commodities": ["electronics", "machinery", "textiles", "footwear", "rice", "coffee"],
        "is_landlocked": False,
        "sealane_adjacency": ["south_china_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.025,
    },
    {
        "country_code": "IND",
        "country_name_ja": "インド",
        "region": "南アジア",
        "democracy_index": 7.04,
        "corruption_index": 39.0,
        "rule_of_law": 0.55,
        "gdp_usd_bn": 3550.0,
        "export_concentration": 0.14,
        "top_export_commodities": ["pharmaceuticals", "refined_petroleum", "diamonds", "rice", "machinery", "textiles"],
        "is_landlocked": False,
        "sealane_adjacency": ["indian_ocean", "arabian_sea", "bay_of_bengal"],
        "military_alliances": ["QUAD"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.028,
    },

    # ── 地政学的要衝国 ──────────────────────────────────────────

    {
        "country_code": "CHN",
        "country_name_ja": "中国",
        "region": "東アジア",
        "democracy_index": 1.97,
        "corruption_index": 45.0,
        "rule_of_law": 0.48,
        "gdp_usd_bn": 17795.0,
        "export_concentration": 0.10,
        "top_export_commodities": ["electronics", "machinery", "steel", "rare_earth", "solar_panels", "ev_batteries"],
        "is_landlocked": False,
        "sealane_adjacency": ["south_china_sea", "east_china_sea", "yellow_sea", "pacific_ocean"],
        "military_alliances": ["SCO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.243,
    },
    {
        "country_code": "JPN",
        "country_name_ja": "日本",
        "region": "東アジア",
        "democracy_index": 8.33,
        "corruption_index": 73.0,
        "rule_of_law": 0.82,
        "gdp_usd_bn": 4230.0,
        "export_concentration": 0.12,
        "top_export_commodities": ["automobiles", "electronics", "machinery", "chemicals", "steel"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "east_china_sea", "sea_of_japan", "south_china_sea"],
        "military_alliances": ["QUAD"],
        "sanction_flag": False,
        "trade_dependency_jp": 1.000,
    },
    {
        "country_code": "KOR",
        "country_name_ja": "韓国",
        "region": "東アジア",
        "democracy_index": 8.09,
        "corruption_index": 63.0,
        "rule_of_law": 0.77,
        "gdp_usd_bn": 1709.0,
        "export_concentration": 0.16,
        "top_export_commodities": ["semiconductors", "automobiles", "steel", "ships", "petrochemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["yellow_sea", "east_china_sea", "sea_of_japan"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.045,
    },
    {
        "country_code": "TWN",
        "country_name_ja": "台湾",
        "region": "東アジア",
        "democracy_index": 8.92,
        "corruption_index": 67.0,
        "rule_of_law": 0.80,
        "gdp_usd_bn": 759.0,
        "export_concentration": 0.32,
        "top_export_commodities": ["semiconductors", "electronics", "machinery", "petrochemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["taiwan_strait", "south_china_sea", "pacific_ocean"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.057,
    },
    {
        "country_code": "SGP",
        "country_name_ja": "シンガポール",
        "region": "東南アジア",
        "democracy_index": 6.22,
        "corruption_index": 83.0,
        "rule_of_law": 0.84,
        "gdp_usd_bn": 501.0,
        "export_concentration": 0.22,
        "top_export_commodities": ["electronics", "refined_petroleum", "machinery", "chemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_malacca", "south_china_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.030,
    },
    {
        "country_code": "MYS",
        "country_name_ja": "マレーシア",
        "region": "東南アジア",
        "democracy_index": 7.30,
        "corruption_index": 50.0,
        "rule_of_law": 0.60,
        "gdp_usd_bn": 430.0,
        "export_concentration": 0.25,
        "top_export_commodities": ["electronics", "palm_oil", "lng", "crude_oil", "rubber"],
        "is_landlocked": False,
        "sealane_adjacency": ["strait_of_malacca", "south_china_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.020,
    },
    {
        "country_code": "EGY",
        "country_name_ja": "エジプト",
        "region": "中東",
        "democracy_index": 2.93,
        "corruption_index": 35.0,
        "rule_of_law": 0.40,
        "gdp_usd_bn": 395.0,
        "export_concentration": 0.30,
        "top_export_commodities": ["refined_petroleum", "natural_gas", "cotton", "gold"],
        "is_landlocked": False,
        "sealane_adjacency": ["suez_canal", "red_sea", "mediterranean_sea"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.003,
    },
    {
        "country_code": "TUR",
        "country_name_ja": "トルコ",
        "region": "中東",
        "democracy_index": 4.35,
        "corruption_index": 34.0,
        "rule_of_law": 0.44,
        "gdp_usd_bn": 1108.0,
        "export_concentration": 0.15,
        "top_export_commodities": ["machinery", "automobiles", "textiles", "steel", "chemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["turkish_strait", "black_sea", "mediterranean_sea"],
        "military_alliances": ["NATO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.005,
    },
    {
        "country_code": "ISR",
        "country_name_ja": "イスラエル",
        "region": "中東",
        "democracy_index": 7.84,
        "corruption_index": 62.0,
        "rule_of_law": 0.72,
        "gdp_usd_bn": 522.0,
        "export_concentration": 0.25,
        "top_export_commodities": ["diamonds", "electronics", "machinery", "chemicals", "pharmaceuticals"],
        "is_landlocked": False,
        "sealane_adjacency": ["mediterranean_sea", "red_sea"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.004,
    },
    {
        "country_code": "PAK",
        "country_name_ja": "パキスタン",
        "region": "南アジア",
        "democracy_index": 4.26,
        "corruption_index": 29.0,
        "rule_of_law": 0.39,
        "gdp_usd_bn": 338.0,
        "export_concentration": 0.30,
        "top_export_commodities": ["textiles", "rice", "leather", "surgical_instruments"],
        "is_landlocked": False,
        "sealane_adjacency": ["arabian_sea", "indian_ocean"],
        "military_alliances": ["SCO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },

    # ── 欧州主要国 ─────────────────────────────────────────────

    {
        "country_code": "DEU",
        "country_name_ja": "ドイツ",
        "region": "欧州",
        "democracy_index": 8.68,
        "corruption_index": 78.0,
        "rule_of_law": 0.87,
        "gdp_usd_bn": 4457.0,
        "export_concentration": 0.10,
        "top_export_commodities": ["automobiles", "machinery", "chemicals", "electronics", "pharmaceuticals"],
        "is_landlocked": False,
        "sealane_adjacency": ["north_sea", "baltic_sea"],
        "military_alliances": ["NATO", "EU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.035,
    },
    {
        "country_code": "FRA",
        "country_name_ja": "フランス",
        "region": "欧州",
        "democracy_index": 8.07,
        "corruption_index": 71.0,
        "rule_of_law": 0.80,
        "gdp_usd_bn": 3031.0,
        "export_concentration": 0.11,
        "top_export_commodities": ["aircraft", "pharmaceuticals", "machinery", "automobiles", "wine"],
        "is_landlocked": False,
        "sealane_adjacency": ["atlantic_ocean", "mediterranean_sea", "english_channel"],
        "military_alliances": ["NATO", "EU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.019,
    },
    {
        "country_code": "GBR",
        "country_name_ja": "イギリス",
        "region": "欧州",
        "democracy_index": 8.28,
        "corruption_index": 71.0,
        "rule_of_law": 0.83,
        "gdp_usd_bn": 3099.0,
        "export_concentration": 0.12,
        "top_export_commodities": ["machinery", "pharmaceuticals", "automobiles", "aircraft", "financial_services"],
        "is_landlocked": False,
        "sealane_adjacency": ["atlantic_ocean", "north_sea", "english_channel"],
        "military_alliances": ["NATO", "AUKUS"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.024,
    },
    {
        "country_code": "NLD",
        "country_name_ja": "オランダ",
        "region": "欧州",
        "democracy_index": 9.00,
        "corruption_index": 79.0,
        "rule_of_law": 0.88,
        "gdp_usd_bn": 1119.0,
        "export_concentration": 0.14,
        "top_export_commodities": ["machinery", "chemicals", "semiconductors", "petroleum_products", "food"],
        "is_landlocked": False,
        "sealane_adjacency": ["north_sea", "english_channel"],
        "military_alliances": ["NATO", "EU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.016,
    },
    {
        "country_code": "POL",
        "country_name_ja": "ポーランド",
        "region": "欧州",
        "democracy_index": 6.80,
        "corruption_index": 55.0,
        "rule_of_law": 0.62,
        "gdp_usd_bn": 809.0,
        "export_concentration": 0.13,
        "top_export_commodities": ["machinery", "automobiles", "electronics", "food", "chemicals"],
        "is_landlocked": False,
        "sealane_adjacency": ["baltic_sea"],
        "military_alliances": ["NATO", "EU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.005,
    },

    # ── その他重要国 ────────────────────────────────────────────

    {
        "country_code": "MEX",
        "country_name_ja": "メキシコ",
        "region": "北米",
        "democracy_index": 6.70,
        "corruption_index": 31.0,
        "rule_of_law": 0.43,
        "gdp_usd_bn": 1424.0,
        "export_concentration": 0.16,
        "top_export_commodities": ["automobiles", "electronics", "crude_oil", "machinery", "silver"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "gulf_of_mexico", "atlantic_ocean"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.012,
    },
    {
        "country_code": "ZMB",
        "country_name_ja": "ザンビア",
        "region": "アフリカ",
        "democracy_index": 5.62,
        "corruption_index": 33.0,
        "rule_of_law": 0.42,
        "gdp_usd_bn": 29.0,
        "export_concentration": 0.75,
        "top_export_commodities": ["copper", "cobalt", "gold", "tobacco"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "MOZ",
        "country_name_ja": "モザンビーク",
        "region": "アフリカ",
        "democracy_index": 4.89,
        "corruption_index": 26.0,
        "rule_of_law": 0.40,
        "gdp_usd_bn": 18.0,
        "export_concentration": 0.65,
        "top_export_commodities": ["lng", "coal", "aluminum", "prawns"],
        "is_landlocked": False,
        "sealane_adjacency": ["indian_ocean", "mozambique_channel"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "PNG",
        "country_name_ja": "パプアニューギニア",
        "region": "オセアニア",
        "democracy_index": 6.03,
        "corruption_index": 27.0,
        "rule_of_law": 0.38,
        "gdp_usd_bn": 30.0,
        "export_concentration": 0.60,
        "top_export_commodities": ["lng", "gold", "copper", "palm_oil", "timber"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "coral_sea", "bismarck_sea"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.004,
    },
    {
        "country_code": "UZB",
        "country_name_ja": "ウズベキスタン",
        "region": "中央アジア",
        "democracy_index": 2.12,
        "corruption_index": 33.0,
        "rule_of_law": 0.38,
        "gdp_usd_bn": 100.0,
        "export_concentration": 0.45,
        "top_export_commodities": ["gold", "uranium", "cotton", "natural_gas", "copper"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["SCO"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "MMR",
        "country_name_ja": "ミャンマー",
        "region": "東南アジア",
        "democracy_index": 1.02,
        "corruption_index": 20.0,
        "rule_of_law": 0.26,
        "gdp_usd_bn": 65.0,
        "export_concentration": 0.40,
        "top_export_commodities": ["natural_gas", "jade", "gems", "rice", "beans", "rare_earth"],
        "is_landlocked": False,
        "sealane_adjacency": ["andaman_sea", "bay_of_bengal"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": True,
        "trade_dependency_jp": 0.003,
    },
    {
        "country_code": "KHM",
        "country_name_ja": "カンボジア",
        "region": "東南アジア",
        "democracy_index": 3.43,
        "corruption_index": 23.0,
        "rule_of_law": 0.37,
        "gdp_usd_bn": 31.0,
        "export_concentration": 0.38,
        "top_export_commodities": ["garments", "footwear", "rice", "rubber", "fish"],
        "is_landlocked": False,
        "sealane_adjacency": ["gulf_of_thailand", "south_china_sea"],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.006,
    },
    {
        "country_code": "LAO",
        "country_name_ja": "ラオス",
        "region": "東南アジア",
        "democracy_index": 2.06,
        "corruption_index": 30.0,
        "rule_of_law": 0.35,
        "gdp_usd_bn": 15.0,
        "export_concentration": 0.50,
        "top_export_commodities": ["electricity", "copper", "gold", "timber", "coffee"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["ASEAN"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.002,
    },
    {
        "country_code": "BGD",
        "country_name_ja": "バングラデシュ",
        "region": "南アジア",
        "democracy_index": 5.99,
        "corruption_index": 25.0,
        "rule_of_law": 0.40,
        "gdp_usd_bn": 460.0,
        "export_concentration": 0.55,
        "top_export_commodities": ["garments", "textiles", "fish", "leather"],
        "is_landlocked": False,
        "sealane_adjacency": ["bay_of_bengal", "indian_ocean"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.004,
    },
    {
        "country_code": "COL",
        "country_name_ja": "コロンビア",
        "region": "中南米",
        "democracy_index": 6.78,
        "corruption_index": 39.0,
        "rule_of_law": 0.50,
        "gdp_usd_bn": 363.0,
        "export_concentration": 0.42,
        "top_export_commodities": ["crude_oil", "coal", "coffee", "emeralds", "gold", "nickel"],
        "is_landlocked": False,
        "sealane_adjacency": ["pacific_ocean", "caribbean_sea"],
        "military_alliances": [],
        "sanction_flag": False,
        "trade_dependency_jp": 0.003,
    },
    {
        "country_code": "VEN",
        "country_name_ja": "ベネズエラ",
        "region": "中南米",
        "democracy_index": 2.16,
        "corruption_index": 13.0,
        "rule_of_law": 0.22,
        "gdp_usd_bn": 97.0,
        "export_concentration": 0.90,
        "top_export_commodities": ["crude_oil", "petroleum_products", "gold"],
        "is_landlocked": False,
        "sealane_adjacency": ["caribbean_sea", "atlantic_ocean"],
        "military_alliances": [],
        "sanction_flag": True,
        "trade_dependency_jp": 0.000,
    },
    {
        "country_code": "ETH",
        "country_name_ja": "エチオピア",
        "region": "アフリカ",
        "democracy_index": 3.44,
        "corruption_index": 37.0,
        "rule_of_law": 0.38,
        "gdp_usd_bn": 164.0,
        "export_concentration": 0.55,
        "top_export_commodities": ["coffee", "gold", "leather", "sesame"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "TZA",
        "country_name_ja": "タンザニア",
        "region": "アフリカ",
        "democracy_index": 5.16,
        "corruption_index": 40.0,
        "rule_of_law": 0.45,
        "gdp_usd_bn": 84.0,
        "export_concentration": 0.48,
        "top_export_commodities": ["gold", "tanzanite", "tobacco", "coffee", "fish", "nickel"],
        "is_landlocked": False,
        "sealane_adjacency": ["indian_ocean"],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
    {
        "country_code": "ZWE",
        "country_name_ja": "ジンバブエ",
        "region": "アフリカ",
        "democracy_index": 3.16,
        "corruption_index": 23.0,
        "rule_of_law": 0.32,
        "gdp_usd_bn": 26.0,
        "export_concentration": 0.55,
        "top_export_commodities": ["platinum", "gold", "tobacco", "diamonds", "lithium"],
        "is_landlocked": True,
        "sealane_adjacency": [],
        "military_alliances": ["AU"],
        "sanction_flag": False,
        "trade_dependency_jp": 0.001,
    },
]


# ==============================================================
# BLOCK 2: BQクライアント接続ユーティリティ
# ==============================================================

def _get_bq_client():
    """
    edinet_bq_client.EDINETBQClient を動的 import して返す。
    import 失敗時は None を返す（フォールバック保護）。
    """
    try:
        import importlib
        import sys
        from pathlib import Path
        # Colab 環境での検索パスを補完
        for p in [
            "/content",
            str(Path(__file__).parent),
        ]:
            if p not in sys.path:
                sys.path.insert(0, p)
        mod = importlib.import_module("edinet_bq_client")
        return mod.EDINETBQClient()
    except Exception as e:
        print(f"  ⚠️  edinet_bq_client import 失敗: {e}")
        return None


# ==============================================================
# BLOCK 3: MERGE実行（INSERT OR UPDATE相当）
# ==============================================================

def _build_merge_query(project_id: str, rows: list[dict]) -> str:
    """
    geopolitics.country_profile への MERGE クエリを生成する。
    country_code を主キーとして UPSERT する。

    BigQuery MERGE 構文:
      MERGE target USING source ON key
      WHEN MATCHED THEN UPDATE
      WHEN NOT MATCHED THEN INSERT
    """
    if not rows:
        return ""

    # VALUES 行を構築
    value_rows = []
    for r in rows:
        def _s(v):
            """文字列をシングルクォートでエスケープ"""
            if v is None:
                return "NULL"
            return "'" + str(v).replace("'", "\\'") + "'"

        def _f(v):
            """float/None → SQL表現"""
            if v is None:
                return "NULL"
            return str(float(v))

        def _b(v):
            """bool → SQL表現"""
            if v is None:
                return "NULL"
            return "TRUE" if v else "FALSE"

        def _j(v):
            """list/dict → JSON文字列としてSQL表現"""
            if v is None:
                return "NULL"
            return "'" + json.dumps(v, ensure_ascii=False).replace("'", "\\'") + "'"

        value_rows.append(
            f"  ({_s(r['country_code'])}, {_s(r['country_name_ja'])}, "
            f"{_s(r['region'])}, {_f(r['democracy_index'])}, "
            f"{_f(r['corruption_index'])}, {_f(r['rule_of_law'])}, "
            f"{_f(r['gdp_usd_bn'])}, {_f(r['export_concentration'])}, "
            f"{_j(r['top_export_commodities'])}, {_b(r['is_landlocked'])}, "
            f"{_j(r['sealane_adjacency'])}, {_j(r['military_alliances'])}, "
            f"{_b(r['sanction_flag'])}, {_f(r['trade_dependency_jp'])}, "
            f"CURRENT_TIMESTAMP())"
        )

    values_sql = ",\n".join(value_rows)

    return f"""
MERGE `{project_id}.geopolitics.country_profile` AS T
USING (
  SELECT * FROM UNNEST([
    STRUCT
      country_code STRING,
      country_name_ja STRING,
      region STRING,
      democracy_index FLOAT64,
      corruption_index FLOAT64,
      rule_of_law FLOAT64,
      gdp_usd_bn FLOAT64,
      export_concentration FLOAT64,
      top_export_commodities JSON,
      is_landlocked BOOL,
      sealane_adjacency JSON,
      military_alliances JSON,
      sanction_flag BOOL,
      trade_dependency_jp FLOAT64,
      updated_at TIMESTAMP
    >
{values_sql}
  ])
) AS S
ON T.country_code = S.country_code
WHEN MATCHED THEN
  UPDATE SET
    T.country_name_ja        = S.country_name_ja,
    T.region                 = S.region,
    T.democracy_index        = S.democracy_index,
    T.corruption_index       = S.corruption_index,
    T.rule_of_law            = S.rule_of_law,
    T.gdp_usd_bn             = S.gdp_usd_bn,
    T.export_concentration   = S.export_concentration,
    T.top_export_commodities = S.top_export_commodities,
    T.is_landlocked          = S.is_landlocked,
    T.sealane_adjacency      = S.sealane_adjacency,
    T.military_alliances     = S.military_alliances,
    T.sanction_flag          = S.sanction_flag,
    T.trade_dependency_jp    = S.trade_dependency_jp,
    T.updated_at             = S.updated_at
WHEN NOT MATCHED THEN
  INSERT (
    country_code, country_name_ja, region,
    democracy_index, corruption_index, rule_of_law,
    gdp_usd_bn, export_concentration,
    top_export_commodities, is_landlocked,
    sealane_adjacency, military_alliances,
    sanction_flag, trade_dependency_jp, updated_at
  )
  VALUES (
    S.country_code, S.country_name_ja, S.region,
    S.democracy_index, S.corruption_index, S.rule_of_law,
    S.gdp_usd_bn, S.export_concentration,
    S.top_export_commodities, S.is_landlocked,
    S.sealane_adjacency, S.military_alliances,
    S.sanction_flag, S.trade_dependency_jp, S.updated_at
  )
"""


# ==============================================================
# BLOCK 4: メインエントリポイント
# ==============================================================

def build_country_profiles(
    project_id: str = "stock-analyzer-edinet",
    dry_run: bool = False,
    target_codes: list[str] | None = None,
) -> dict:
    """
    geopolitics.country_profile テーブルに静的マスターデータを MERGE する。

    Args:
        project_id   : GCPプロジェクトID
        dry_run      : True の場合 BQ 実行せず件数のみ表示
        target_codes : 指定した場合は対象 country_code のみ処理（テスト用）

    Returns:
        {
          "total":       int,   # 処理対象件数
          "success":     bool,  # MERGE成功フラグ
          "error":       str,   # エラーメッセージ（正常時は空文字）
          "timestamp":   str,
        }
    """
    print("\n🌍 country_profile_builder: 国別プロファイル構築開始")
    print(f"   対象テーブル: {project_id}.geopolitics.country_profile")

    rows = COUNTRY_PROFILES.copy()

    # フィルタリング（テスト用部分実行）
    if target_codes:
        rows = [r for r in rows if r["country_code"] in target_codes]
        print(f"   絞り込み指定: {target_codes}")

    print(f"   対象国数: {len(rows)} 件")

    if dry_run:
        print("   [DRY RUN] BQ実行をスキップ")
        for r in rows:
            print(f"   → {r['country_code']}: {r['country_name_ja']} "
                  f"({r['region']}) "
                  f"制裁={r['sanction_flag']} "
                  f"内陸={r['is_landlocked']}")
        return {
            "total":     len(rows),
            "success":   True,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # BQ クライアント取得
    bq_client = _get_bq_client()
    if bq_client is None:
        return {
            "total":     len(rows),
            "success":   False,
            "error":     "edinet_bq_client import 失敗",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # raw BQ client を取得（EDINETBQClient が内包する google.cloud.bigquery.Client）
    try:
        raw_bq = bq_client.client  # EDINETBQClient.client が BigQuery Client
    except AttributeError:
        # フォールバック: google.cloud.bigquery を直接使用
        try:
            from google.cloud import bigquery
            from google.colab import auth as _auth
            _auth.authenticate_user()
            raw_bq = bigquery.Client(
                project=project_id,
                location="asia-northeast1",
            )
        except Exception as e:
            return {
                "total":     len(rows),
                "success":   False,
                "error":     f"BQ Client 取得失敗: {e}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    # MERGE クエリを生成・実行
    try:
        merge_sql = _build_merge_query(project_id, rows)
        job = raw_bq.query(merge_sql)
        job.result()  # 同期実行（完了まで待機）

        print(f"   ✅ MERGE 完了: {len(rows)} 件")
        print(f"   JOB ID: {job.job_id}")
        return {
            "total":     len(rows),
            "success":   True,
            "error":     "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        err = str(e)
        print(f"   ❌ MERGE 失敗: {err[:200]}")
        return {
            "total":     len(rows),
            "success":   False,
            "error":     err[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ==============================================================
# BLOCK 5: 参照ユーティリティ（他モジュールから呼ばれる）
# ==============================================================

def get_country_profile(country_code: str) -> dict | None:
    """
    静的マスターから country_code に一致するプロファイルを返す。
    BQ 不要のローカル参照用（geo_signal.py 等から利用）。

    Args:
        country_code: ISO 3166-1 alpha-3（例: "SAU"）

    Returns:
        dict | None
    """
    for p in COUNTRY_PROFILES:
        if p["country_code"] == country_code:
            return p.copy()
    return None


def get_all_country_codes() -> list[str]:
    """全監視対象国コードのリストを返す。"""
    return [p["country_code"] for p in COUNTRY_PROFILES]


def get_sanctioned_countries() -> list[str]:
    """制裁対象国コードのリストを返す。"""
    return [p["country_code"] for p in COUNTRY_PROFILES if p["sanction_flag"]]


def get_countries_by_commodity(commodity_code: str) -> list[str]:
    """
    指定資源コードを主要輸出品に持つ国コードのリストを返す。

    Args:
        commodity_code: 例 "crude_oil" / "copper" / "lng"

    Returns:
        list[str]: 該当する country_code のリスト
    """
    return [
        p["country_code"]
        for p in COUNTRY_PROFILES
        if commodity_code in p.get("top_export_commodities", [])
    ]


def get_countries_by_sealane(sealane_code: str) -> list[str]:
    """
    指定シーレーンに隣接する国コードのリストを返す。

    Args:
        sealane_code: 例 "strait_of_hormuz" / "strait_of_malacca"

    Returns:
        list[str]: 該当する country_code のリスト
    """
    return [
        p["country_code"]
        for p in COUNTRY_PROFILES
        if sealane_code in p.get("sealane_adjacency", [])
    ]


# ==============================================================
# 単体テスト / Colab実行エントリ
# ==============================================================

if __name__ == "__main__":
    import sys

    # --dry-run フラグを解析
    dry_run_flag = "--dry-run" in sys.argv
    target_flag  = None
    for i, arg in enumerate(sys.argv):
        if arg == "--target" and i + 1 < len(sys.argv):
            target_flag = sys.argv[i + 1].split(",")

    print("=" * 60)
    print("country_profile_builder.py v1.0")
    print("=" * 60)

    # ── 統計サマリー ─────────────────────────────────────────
    total      = len(COUNTRY_PROFILES)
    sanctioned = len(get_sanctioned_countries())
    landlocked = sum(1 for p in COUNTRY_PROFILES if p["is_landlocked"])
    hormuz     = len(get_countries_by_sealane("strait_of_hormuz"))
    malacca    = len(get_countries_by_sealane("strait_of_malacca"))
    crude_oil  = len(get_countries_by_commodity("crude_oil"))

    print(f"\n📊 マスターデータ統計:")
    print(f"   総国数:              {total}")
    print(f"   制裁対象国:          {sanctioned} ({get_sanctioned_countries()})")
    print(f"   内陸国:              {landlocked}")
    print(f"   ホルムズ海峡隣接国:  {hormuz}")
    print(f"   マラッカ海峡隣接国:  {malacca}")
    print(f"   原油輸出国:          {crude_oil}")

    # ── BQ MERGE 実行 ────────────────────────────────────────
    result = build_country_profiles(
        project_id   = "stock-analyzer-edinet",
        dry_run      = dry_run_flag,
        target_codes = target_flag,
    )

    print(f"\n{'✅ 完了' if result['success'] else '❌ 失敗'}")
    print(f"   処理件数: {result['total']}")
    if result["error"]:
        print(f"   エラー: {result['error']}")
    print(f"   実行時刻: {result['timestamp']}")