"""国務省の渡航情報 RSS：レベルと国名はタイトルだけから取る（本文の語に引きずられない）。"""
import country_risk_updater as c


def _item(title, desc=""):
    return f"<item><title>{title}</title><description><![CDATA[{desc}]]></description></item>"


def test_level_and_country_come_from_title_only():
    xml = "<rss><channel>" + "".join([
        # 本文に Level 4・United States・Russia が出ても、タイトルの国・レベルを使う
        _item("Thailand - Level 1: Exercise Normal Precautions",
              "Level 4: Do Not Travel to some areas. U.S. citizens (United States) ... Russia"),
        _item("Ukraine - Level 4: Do Not Travel", "Russia's war ..."),
        _item("Venezuela - Level 4: Do Not Travel", "The United States has ..."),
        _item("Papua New Guinea - Level 2: Exercise Increased Caution"),
        _item("Guinea-Bissau - Level 3: Reconsider Travel"),
        _item("Guinea - Level 2: Exercise Increased Caution"),
        _item("Burma (Myanmar) - Level 4: Do Not Travel"),
        _item("Israel, the West Bank and Gaza - Level 3: Reconsider Travel"),
        _item("T&#252;rkiye - Level 2: Exercise Increased Caution"),
        _item("Worldwide Caution", "Level 4 ... United States"),
    ]) + "</channel></rss>"
    lv = c._parse_state_dept_rss(xml)
    assert lv["THA"] == 1 and lv["UKR"] == 4 and lv["VEN"] == 4
    assert lv["PNG"] == 2 and lv["GIN"] == 2          # Guinea-Bissau は Guinea に混ぜない
    assert lv["MMR"] == 4 and lv["ISR"] == 3 and lv["TUR"] == 2
    assert "USA" not in lv and "RUS" not in lv


def test_country_head_exact_or_segment():
    assert c._state_country_of("Korea, South") == "KOR"
    assert c._state_country_of("Mainland China, Hong Kong & Macau") == "CHN"
    assert c._state_country_of("Equatorial Guinea") is None
    assert c._state_country_of("United States") is None


def test_congo_title_order_variant():
    xml = "<rss>" + _item("Congo, Democratic Republic of the - Level 3: Reconsider Travel") + "</rss>"
    assert c._parse_state_dept_rss(xml) == {"COD": 3}
