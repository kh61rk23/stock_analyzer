"""
U-2（SEC User-Agent 応急是正 / F-2）の回帰テスト。

対象: institutional_13f_monitor.py / us_insider_form4_scraper.py /
      sec_geographic_extractor.py / us_sector_bridge_enhanced.py
      （新規に `_sec_user_agent(cfg=None)` を実装）
参照実装: us_8k_monitor.py:116-122 の既存 `_sec_user_agent()`（引数なし）

検証内容:
  1. 環境変数 SEC_USER_AGENT 設定時、5モジュールすべてがその値を返す。
  2. 環境変数なし・cfg あり のとき、4モジュール（cfg引数を持つもの）は
     cfg.SEC_USER_AGENT を返す。
  3. 環境変数なし・cfg なし（または cfg.SEC_USER_AGENT 不在）のとき、
     5モジュールすべてが統一フォールバック文字列
     "StockAnalyzer stockanalyzer@example.com" を返す。
  4. 将来の再発防止: 4ファイル + us_8k_monitor.py のソースに
     "example.com" を含む文字列リテラルが、統一フォールバック値と異なる
     ものが存在しないことを AST で機械検査する（ダミーUAの再混入を検知する）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import institutional_13f_monitor
import sec_geographic_extractor
import us_8k_monitor
import us_insider_form4_scraper
import us_sector_bridge_enhanced

REPO = Path(__file__).resolve().parent.parent

FALLBACK_UA = "StockAnalyzer stockanalyzer@example.com"

# cfg 引数を受け取れる4モジュール（R2-2 対象）
MODULES_WITH_CFG = [
    institutional_13f_monitor,
    us_insider_form4_scraper,
    sec_geographic_extractor,
    us_sector_bridge_enhanced,
]

# grep/機械検査の対象5ファイル（4ファイル + 参照実装の us_8k_monitor.py）
TARGET_FILES = [
    "institutional_13f_monitor.py",
    "us_insider_form4_scraper.py",
    "sec_geographic_extractor.py",
    "us_sector_bridge_enhanced.py",
    "us_8k_monitor.py",
]


# ==============================================================
# 1. 環境変数優先
# ==============================================================

def test_env_var_takes_priority_for_4_modules(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "custom-ua-from-env")
    for mod in MODULES_WITH_CFG:
        assert mod._sec_user_agent() == "custom-ua-from-env"
        # cfg が指定されていても環境変数が最優先される
        cfg = SimpleNamespace(SEC_USER_AGENT="should-be-ignored")
        assert mod._sec_user_agent(cfg) == "custom-ua-from-env"


def test_env_var_takes_priority_for_us_8k_monitor(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "custom-ua-from-env")
    assert us_8k_monitor._sec_user_agent() == "custom-ua-from-env"


# ==============================================================
# 2. 環境変数なし・cfg あり → cfg.SEC_USER_AGENT
# ==============================================================

def test_cfg_value_used_when_no_env_var(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cfg = SimpleNamespace(SEC_USER_AGENT="cfg-provided-ua")
    for mod in MODULES_WITH_CFG:
        assert mod._sec_user_agent(cfg) == "cfg-provided-ua"


# ==============================================================
# 3. 環境変数なし・cfg なし（または SEC_USER_AGENT 不在）→ フォールバック
# ==============================================================

def test_fallback_when_cfg_none_and_no_env_var(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    for mod in MODULES_WITH_CFG:
        # cfg=None のときは各モジュール内部で `import config` して
        # config.SEC_USER_AGENT を見る。実運用の config.py は正しい値を
        # 持つため、フォールバック分岐を確実に踏ませるには
        # config.SEC_USER_AGENT を一時的に空にする。
        import config
        monkeypatch.setattr(config, "SEC_USER_AGENT", "", raising=False)
        assert mod._sec_user_agent() == FALLBACK_UA


def test_fallback_when_cfg_has_no_sec_user_agent_attr(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cfg = SimpleNamespace()  # SEC_USER_AGENT 属性なし
    for mod in MODULES_WITH_CFG:
        assert mod._sec_user_agent(cfg) == FALLBACK_UA


def test_fallback_for_us_8k_monitor(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    import config
    monkeypatch.setattr(config, "SEC_USER_AGENT", "", raising=False)
    assert us_8k_monitor._sec_user_agent() == FALLBACK_UA


# ==============================================================
# 4. 機械検査: example.com を含むリテラルが統一フォールバック値以外に無い
# ==============================================================

def _string_literals_containing(source: str, needle: str) -> set[str]:
    """ソース中の文字列リテラル（f-string の定数部分も含む）のうち
    needle を含むものの集合を返す。"""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if needle in node.value:
                found.add(node.value)
    return found


def test_no_stray_example_com_literals():
    """
    各ファイルに example.com を含む文字列リテラルが存在する場合、
    それは必ず統一フォールバック文字列
    "StockAnalyzer stockanalyzer@example.com" と完全一致すること。
    別のダミーアドレス（contact@example.com 等）が再混入していないかを
    将来にわたって検知する。
    """
    for filename in TARGET_FILES:
        path = REPO / filename
        source = path.read_text(encoding="utf-8")
        literals = _string_literals_containing(source, "example.com")
        unexpected = literals - {FALLBACK_UA}
        assert not unexpected, (
            f"{filename} に統一フォールバック値と異なる example.com リテラルが"
            f"見つかりました: {unexpected}"
        )


def test_4_target_files_have_exactly_one_fallback_literal_occurrence():
    """
    R2-2の実装方式（名前付き定数を1箇所で定義し、分岐から参照する）に
    従っていることを、grep 相当の単純な出現回数でも確認する。
    us_8k_monitor.py は対象外（同一リテラルを2箇所に直接書く既存実装のため、
    出現回数チェックはこの4ファイルにのみ適用する）。
    """
    for filename in TARGET_FILES:
        if filename == "us_8k_monitor.py":
            continue
        path = REPO / filename
        source = path.read_text(encoding="utf-8")
        occurrences = source.count("example.com")
        assert occurrences == 1, (
            f"{filename}: example.com の出現回数が {occurrences} 件です"
            "（フォールバック定数1箇所のみを想定）"
        )


def test_no_hardcoded_user_agent_dict_literal():
    """
    4ファイルに '"User-Agent": "<文字列リテラル>"' の形のハードコードされた
    辞書エントリが残っていないこと（関数呼び出し経由になっていること）。
    """
    import re

    pattern = re.compile(r'"User-Agent"\s*:\s*"')
    for filename in TARGET_FILES:
        if filename == "us_8k_monitor.py":
            continue
        path = REPO / filename
        source = path.read_text(encoding="utf-8")
        assert not pattern.search(source), (
            f"{filename} にハードコードされた User-Agent 辞書リテラルが残っています"
        )
