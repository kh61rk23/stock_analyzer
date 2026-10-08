# country_risk_updater — GDELT BigQuery経路＋合成スコア再正規化（純粋ロジック）
#   ② 国務省RSSがGitHub Actions IPで恒常403 → 取得失敗時は合成スコアを
#      残ソースで再正規化する（mofa_available / state_available フラグ）。
#   ③ GDELT Doc APIが恒常429 → BigQuery公開データセット(gdelt-bq.gdeltv2)
#      経路をprimary化。ISO3→FIPSマッピングとトーン変換の純粋関数を検証する。
#   ネットワーク・BQ接続には依存しない。
from conftest import load_module

cru = load_module("country_risk_updater")


class TestToneToInstability:
    def test_negative_tone_scales_by_5(self):
        # avg_tone=-5 → instability = 5 × 5.0 = 25
        assert cru.tone_to_instability(-5.0) == 25.0

    def test_positive_tone_clamps_to_zero(self):
        assert cru.tone_to_instability(3.2) == 0.0

    def test_zero_tone(self):
        assert cru.tone_to_instability(0.0) == 0.0

    def test_extreme_negative_clips_at_100(self):
        # avg_tone=-30 → 150 → 上限100でクリップ
        assert cru.tone_to_instability(-30.0) == 100.0

    def test_matches_doc_api_formula(self):
        # Doc API版と同一式（$max(0,-tone) \times 5$）であることの回帰確認
        assert cru.tone_to_instability(-8.4) == 42.0


class TestIso3ToFipsMap:
    def test_covers_all_gdelt_countries(self):
        # GDELT_COUNTRY_QUERY_MAP の全59国が FIPS マッピングを持つこと
        # （欠けた国は BQ経路で常に取得対象外となり silent 欠損になる）
        missing = [c for c in cru.GDELT_COUNTRY_QUERY_MAP if c not in cru._ISO3_TO_FIPS]
        assert missing == []

    def test_fips_codes_are_unique(self):
        # FIPS 値の重複は「別国のスコアを取り込む」事故になるため禁止
        vals = list(cru._ISO3_TO_FIPS.values())
        assert len(vals) == len(set(vals))

    def test_known_tricky_codes(self):
        # ISO2 と食い違う要注意国（BQ実データ59/59国で検証済み）
        assert cru._ISO3_TO_FIPS["JPN"] == "JA"
        assert cru._ISO3_TO_FIPS["CHN"] == "CH"
        assert cru._ISO3_TO_FIPS["RUS"] == "RS"
        assert cru._ISO3_TO_FIPS["UKR"] == "UP"
        assert cru._ISO3_TO_FIPS["ZMB"] == "ZA"   # FIPS ZA はザンビア（南アではない）
        assert cru._ISO3_TO_FIPS["ZAF"] == "SF"


class TestCompositeRenormalization:
    def test_all_sources_available_unchanged(self):
        # 全ソース取得時は再正規化しない: gdelt=40, mofa=4(→100), state=2(→50)
        # composite = 40×0.5 + 100×0.25 + 50×0.25 = 57.5
        assert cru._calc_composite_risk(40.0, 4, 2) == 57.5

    def test_state_unavailable_renormalizes(self):
        # 国務省RSS取得失敗時: gdelt=40, mofa=4(→100)
        # composite = (40×0.5 + 100×0.25) / 0.75 = 60.0
        assert cru._calc_composite_risk(
            40.0, 4, 0, state_available=False) == 60.0

    def test_state_level_zero_with_fetch_success_stays_data(self):
        # RSS取得成功・言及なし（レベル0）は実データとして0のまま計算
        # composite = (40×0.5 + 0×0.25 + 0×0.25) / 1.0 = 20.0
        assert cru._calc_composite_risk(40.0, 0, 0) == 20.0

    def test_gdelt_and_state_missing_mofa_only(self):
        # gdelt=-1（失敗）+ state取得失敗 → mofa 100%
        assert cru._calc_composite_risk(
            -1.0, 2, 0, state_available=False) == 50.0

    def test_all_missing_returns_zero(self):
        assert cru._calc_composite_risk(
            -1.0, 0, 0, mofa_available=False, state_available=False) == 0.0
