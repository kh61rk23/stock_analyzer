"""officer_extractor: 有報「役員の状況」の解析と経営トップ・就任日の判定。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import officer_extractor as oe  # noqa: E402

# PyMuPDF の出力形（氏名と生年月日が別の行・年月と略歴文が別の行）
SECTION = """(2)【役員の状況】
役職名
氏名
生年月日
略歴
任期
所有株式数
(千株)
代表取締役
取締役会議長
秋谷 文男
1940年10月20日
1964年4月当社入社
2004年6月信越半導体(株)代表取締役社長
2023年6月代表取締役取締役会議長(現任)
(注)3
84
代表取締役
社長
斉藤 恭彦
1955年12月5日
1978年4月当社入社
2004年4月
シンエツハンドウタイアメリカInc.取締役社長
2010年6月代表取締役副社長
2016年6月代表取締役社長(現任)
(注)3
167
"""


def test_parse_officers_line_layout():
    offs = oe.parse_officers(oe._norm(SECTION))
    assert [o["name"] for o in offs] == ["秋谷文男", "斉藤恭彦"]
    assert offs[1]["title"] == "代表取締役 社長"
    assert offs[1]["birth"] == "1955-12-05"
    assert offs[1]["careers"][1] == ["2004-04", "シンエツハンドウタイアメリカInc.取締役社長"]


def test_pick_top_ignores_subsidiary_and_vice_president():
    top = oe.pick_top(oe.parse_officers(oe._norm(SECTION)), "信越化学工業株式会社")
    assert top["name"] == "斉藤恭彦"
    assert top["appointed_date"] == "2016-06-01"   # 子会社の社長(2004)・副社長(2010)ではない


def test_inline_layout_and_split_name():
    text = "取締役会長\n(代表取締役) 豊 田 章 男 1956年5月3日生\n2009年6月 同社取締役社長\n(注)3 24,099\n"
    offs = oe.parse_officers(text)
    assert offs[0]["name"] == "豊田章男"
    assert "代表取締役" in offs[0]["title"]


def test_birth_split_over_lines_is_joined():
    pages = ["役員の状況 略歴 生年月日\n代表取締役社長\n鶴見 知久 1966年5月\n11日生\n2022年4月 当社代表取締役社長(現任)\n"]
    offs = oe.parse_officers(oe.officers_section(pages))
    assert offs[0]["birth"] == "1966-05-11"
