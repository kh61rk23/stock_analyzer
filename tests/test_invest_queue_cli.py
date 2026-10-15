"""tools/invest_queue.py の純粋ロジック（YAML・結論の解析・隔週判定）の単体テスト。"""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "invest_queue_cli", Path(__file__).resolve().parent.parent / "tools" / "invest_queue.py")
iq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iq)

REPORT = """---
case_id: 2026-10-20_1234T
ticker: 1234.T
company: テスト商事
verdict: Watch
conviction: 3
layer: カジノ(特定)
bull_conviction: 4
bear_conviction: 3
unresolved: 2
status: 承認待ち
approved_by: 健太
approved_at: 2026-10-21
---
# 投資判断レポート
| # | 項目 | 値 | 定義・読み方 |
|---|---|---|---|
| ① | 事業ポートフォリオ | Ⓕ F1 | x |
| ② | PER | 予想 18.4倍 | y |
"""
BULL = "本文\n\n```yaml\nagent: bull\ncase_id: X\nconviction: 4\nconfidence_pct: 60\nkey_points: [BP-1]\n```\n"
BEAR = "本文\n```yaml\nagent: bear\nconviction: 3\n```\n"
DEVIL = """本文
## 未解消の重大指摘
1. 【反転級】含み益が不明
2. 【精度級】定義不一致
3. 【反転級】前提が不成立
## 次の節
【反転級】は数えない
```yaml
agent: devil
unresolved_critical: 3
```
"""
FILES = {"bull.md": BULL, "bear.md": BEAR, "devil.md": DEVIL, "report.md": REPORT}


def test_frontmatter_and_yaml_blocks():
    assert iq.parse_frontmatter(REPORT)["verdict"] == "Watch"
    assert iq.parse_frontmatter("本文だけ") == {}
    assert iq.memo_conclusion(BULL, "bull")["conviction"] == 4
    assert iq.memo_conclusion(BULL, "bear") == {}
    assert iq.extract_yaml_blocks("```yaml\n: : bad\n  - [\n```") == []


def test_count_reversal_only_inside_unresolved_section():
    assert iq.count_reversal(DEVIL) == 2
    assert iq.count_reversal("節なし") == 0


def test_parse_kpi_rows():
    kpi = iq.parse_kpi(REPORT)
    assert kpi["② PER"] == "予想 18.4倍" and len(kpi) == 2


def test_parse_deliberation_full_and_approval_always_blank():
    row = iq.parse_deliberation(FILES, {"ticker": "9999.T", "sector_code": "1015", "packet_md": "PK"},
                                now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert (row["verdict"], row["conviction"], row["layer"]) == ("Watch", 3, "カジノ(特定)")
    assert (row["bull_conviction"], row["bear_conviction"]) == (4, 3)
    assert row["devil_unresolved"] == 2 and row["devil_reversal"] == 2
    assert row["ticker"] == "1234.T" and row["company_name"] == "テスト商事"
    assert row["packet_md"] == "PK" and row["run_env"] == "cloud_routine"
    # frontmatter に承認が書かれていても取り込まない（人間だけが更新する）
    assert row["approval_status"] is None and row["approved_by"] is None
    assert row["approved_at"] is None and row["review_on"] is None


def test_parse_deliberation_memo_yaml_fallback_and_overrides():
    rep = "---\ncase_id: C\nverdict: Buy\nconviction: 4\n---\nx"
    row = iq.parse_deliberation({**FILES, "report.md": rep, "packet.md": "WORKPK"}, {"ticker": "1.T"},
                                overrides={"layer": "NISA(コア)", "models": {"bull": "sonnet"}})
    assert row["bull_conviction"] == 4 and row["bear_conviction"] == 3      # メモ末尾 YAML から
    assert row["devil_unresolved"] == 3                                       # unresolved_critical から
    assert row["layer"] == "NISA(コア)" and row["models"] == {"bull": "sonnet"}
    assert row["packet_md"] == "WORKPK"


@pytest.mark.parametrize("bad", ["verdict: 買い\nconviction: 3", "verdict: Buy\nconviction: 9",
                                 "verdict: Buy", "conviction: 3"])
def test_parse_deliberation_rejects_bad_conclusion(bad):
    with pytest.raises(ValueError):
        iq.parse_deliberation({**FILES, "report.md": f"---\ncase_id: C\n{bad}\n---\nx"}, {"ticker": "1.T"})


def test_parse_deliberation_requires_all_files():
    with pytest.raises(ValueError, match="devil.md"):
        iq.parse_deliberation({k: v for k, v in FILES.items() if k != "devil.md"})
    with pytest.raises(ValueError, match="bull.md"):
        iq.parse_deliberation({**FILES, "bull.md": "  \n"})


def test_case_id_for():
    assert iq.case_id_for("543A.T", date(2026, 10, 20)) == "2026-10-20_543AT"


def test_due_decision():
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert iq.due_decision(None, now)[0] is True
    assert iq.due_decision(now - timedelta(days=14), now)[0] is True
    assert iq.due_decision(now - timedelta(days=13, hours=23), now)[0] is False
    assert iq.due_decision(datetime(2026, 10, 1), now, 14)[0] is True       # naive は UTC 扱い


def test_read_work_dir(tmp_path):
    for k, v in FILES.items():
        (tmp_path / k).write_text(v, encoding="utf-8")
    (tmp_path / "result.yaml").write_text("layer: 対象外\n", encoding="utf-8")
    files, ov = iq.read_work_dir(tmp_path)
    assert set(files) == set(FILES) and ov == {"layer": "対象外"}
