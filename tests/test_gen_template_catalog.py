"""bi/tools/gen_template_catalog.py：テンプレ JSON から TEMPLATE_CATALOG.md を作る生成物の検査。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bi" / "tools"))
import gen_template_catalog as g  # noqa: E402


def _count():
    return sum(len(json.loads(f.read_text(encoding="utf-8"))) for f in g.TEMPLATE_DIR.glob("*.json"))


def test_render_lists_every_template_and_domain():
    templates = g.load()
    text = g.render(templates)
    assert f"テンプレ {len(templates)} 本" in text
    assert len(templates) == _count()
    for t in templates:
        assert f"`{t['id']}`" in text
    assert "`taxonomy`" in text  # 技術分類を含む


def test_in_progress_domain_is_hidden_as_placeholder():
    templates = g.load()
    text = g.render(templates) if not g.IN_PROGRESS else ""
    # 実装中の領域を指定すると表を出さず、その本数は総数から除かれる
    g.IN_PROGRESS.add("taxonomy")
    try:
        hidden = g.render(templates)
    finally:
        g.IN_PROGRESS.discard("taxonomy")
    assert "tax_node_company_count" not in hidden
    assert "実装中" in hidden
    assert "tax_node_company_count" in g.render(templates) or text == ""


def test_stdout_mode_does_not_write(tmp_path, capsys):
    assert g.main(["--out", "-"]) == 0
    assert capsys.readouterr().out.startswith("# TEMPLATE_CATALOG.md")


def test_file_output(tmp_path):
    out = tmp_path / "sub" / "c.md"
    assert g.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# TEMPLATE_CATALOG.md")


def test_pipe_in_cell_is_escaped():
    assert g.md("a|b\nc") == "a\\|b c"
