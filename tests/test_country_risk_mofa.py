# country_risk_updater の外務省オープンデータ（国別・軽量版）パーサー
# 回帰固定: 旧 anzen.mofa.go.jp の RSS は廃止され HTML(200)を返すため、RSS として読むと
#   foreign_ministry_level が全国 0 のままになった。国別 XML の先頭の riskLevel4〜1 フラグから最大レベルを採る。
# 断片は 2026-11-03 に ezairyu.mofa.go.jp から取得した実データ。
from conftest import load_module

cru = load_module("country_risk_updater")


def _head(l4, l3, l2, l1):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<opendata dataType="L" lastModified="2026/11/03 19:17:53" mailCount="52" odType="04">\n'
        '    <country areaCd="42" countryCd="182"><cd>0380</cd></country>\n'
        f"    <riskLevel4>{l4}</riskLevel4>\n    <riskLevel3>{l3}</riskLevel3>\n"
        f"    <riskLevel2>{l2}</riskLevel2>\n    <riskLevel1>{l1}</riskLevel1>\n"
        "    <infectionLevel4>0</infectionLevel4>\n"
    )


class TestParseMofaCountryXml:
    def test_ukraine_level4(self):
        assert cru._parse_mofa_country_xml(_head(1, 0, 0, 0)) == 4

    def test_partial_regions_take_highest(self):
        # タイ: レベル3・2の地域が混在 → 3
        assert cru._parse_mofa_country_xml(_head(0, 1, 1, 0)) == 3

    def test_no_danger_info_is_zero(self):
        # 英国・米国: 全フラグ 0 = 危険情報なし
        assert cru._parse_mofa_country_xml(_head(0, 0, 0, 0)) == 0

    def test_level1_only(self):
        assert cru._parse_mofa_country_xml(_head(0, 0, 0, 1)) == 1

    def test_error_page_is_none(self):
        # 廃止URLが返すエラーページ（HTML）や空は「取れていない」= None（0 と区別する）
        assert cru._parse_mofa_country_xml("<!doctype html><title>外務省</title>指定されたページが見つかりません。") is None
        assert cru._parse_mofa_country_xml("") is None

    def test_infection_level_ignored(self):
        assert cru._parse_mofa_country_xml(_head(0, 0, 0, 0).replace(
            "<infectionLevel4>0", "<infectionLevel4>1")) == 0


class TestMofaCodes:
    def test_every_profile_country_except_japan_has_code(self):
        # 監視対象国は日本を除き外務省コードを持つ（無いと0固定に戻る）
        missing = [c for c in cru.MOFA_COUNTRY_MAP.values() if c != "JPN" and c not in cru.MOFA_OPENDATA_CODES]
        assert missing == []

    def test_codes_are_four_digits_and_unique(self):
        vals = list(cru.MOFA_OPENDATA_CODES.values())
        assert all(len(v) == 4 and v.isdigit() for v in vals)
        assert len(set(vals)) == len(vals)


class TestFetchMofaLevels:
    def test_fetch_collects_levels_and_reasons(self, monkeypatch):
        monkeypatch.setattr(cru, "_MOFA_FETCH_INTERVAL", 0)
        pages = {
            "0380": (_head(1, 0, 0, 0), ""),
            "0044": (_head(0, 0, 0, 0), ""),
            "0066": (None, "HTTP 403"),
            "0086": ("<html>エラー</html>", ""),
        }
        monkeypatch.setattr(cru, "_fetch_url_head",
                            lambda url, *a, **k: pages[url.rsplit("/", 1)[1][:4]])
        levels, errors = cru._fetch_mofa_levels(["UKR", "GBR", "THA", "CHN"])
        assert levels == {"UKR": 4, "GBR": 0}
        assert errors == {"THA": "HTTP 403", "CHN": "riskLevel 無し（形式違い）"}
