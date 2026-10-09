"""
bi_templates.py
================
【役割】BI基盤のテンプレート（bi/templates/*.json）を読み、検査し、パラメータを埋めたSQLを作る共通部品。
【使い手】tests/test_bi_templates.py ／ bi_export.py（テンプレのコピー前検査）。board 側（DuckDB）と
        Claudeダッシュボード（BigQuery）は同じ規則で置換・組み立てをする。

テンプレSQLは DuckDB と BigQuery の両方で動く書き方に限る。片方にしか無い関数を許すと
「board では動くのにダッシュボードで落ちる」が起きるため、使える関数を許可リストで固定している。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent
TEMPLATE_DIR = REPO / "bi" / "templates"
MART_DIR = REPO / "bi" / "marts"

# 両エンジンで同じ意味になる関数だけ。ここに無い関数呼び出しは検査で落とす。
ALLOWED_FUNCTIONS = {
    "SUM", "AVG", "MIN", "MAX", "COUNT", "COALESCE", "NULLIF", "ROUND", "ABS",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD",
}
# 関数ではなく「( の直前に来る予約語」
SQL_KEYWORDS_BEFORE_PAREN = {
    "IN", "OVER", "AND", "OR", "NOT", "FROM", "JOIN", "ON", "AS", "WHERE", "SELECT", "WITH",
    "EXISTS", "BETWEEN", "USING", "BY", "CASE", "WHEN", "THEN", "ELSE", "END", "HAVING",
    "UNION", "ALL", "BY", "LIMIT", "IS", "ASC", "DESC", "PARTITION", "ORDER", "LEFT", "INNER",
}
# 明示的に禁止（許可リストの外でも理由を示すため）
FORBIDDEN_PATTERNS = [
    (r"\bMEDIAN\b", "MEDIAN"),
    (r"\bQUALIFY\b", "QUALIFY"),
    (r"\bSAFE_DIVIDE\b", "SAFE_DIVIDE"),
    (r"\bAPPROX_\w+", "APPROX_*"),
    (r"\bARRAY_\w+|\bUNNEST\b|\bLIST_\w+|\bSTRING_AGG\b", "配列・集約文字列関数"),
    (r"\bCAST\s*\(", "CAST（DOUBLE化は x * 1.0）"),
    (r"\bAS\s+DOUBLE\b|\bFLOAT64\b", "DOUBLE/FLOAT64 の型名"),
]
VALID_TYPES = {"string", "int", "number"}
VALID_CHARTS = {"bar", "line", "scatter", "table"}
REQUIRED_KEYS = ("id", "domain", "title", "question", "mart", "sql", "params", "chart", "note")

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")
_BAD_STRING_CHARS = re.compile(r"['\"`\;\x00-\x1f]|--|/\*|\*/")
_INT_RE = re.compile(r"^-?\d{1,15}$")


class TemplateError(ValueError):
    """テンプレートの形式・パラメータの不正"""


def load_templates(directory: Path | str = TEMPLATE_DIR) -> list[dict]:
    out: list[dict] = []
    for p in sorted(Path(directory).glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise TemplateError(f"{p.name}: 最上位は配列にしてください")
        out.extend(data)
    return out


def _strip_sql_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def check_dialect(sql: str, marts: set[str]) -> list[str]:
    """両エンジン共通の書き方になっているか。問題の文字列を返す（空なら合格）。"""
    problems: list[str] = []
    body = _strip_sql_comments_and_strings(sql)
    for pat, label in FORBIDDEN_PATTERNS:
        if re.search(pat, body, flags=re.IGNORECASE):
            problems.append(f"禁止: {label}")
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        name = m.group(1).upper()
        if name in ALLOWED_FUNCTIONS or name in SQL_KEYWORDS_BEFORE_PAREN:
            continue
        # CTE 名（name AS ( の形）は関数ではない
        if re.search(rf"\b{re.escape(m.group(1))}\s+AS\s*\(", body, flags=re.IGNORECASE):
            continue
        problems.append(f"許可リスト外の関数: {m.group(1)}")
    ctes = {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\s+AS\s*\(", body, flags=re.IGNORECASE)}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([`\"]?[A-Za-z_][\w.\-`\"]*)", body, flags=re.IGNORECASE):
        t = m.group(1).lower()
        if t not in marts and t not in ctes:
            problems.append(f"マート以外の表を参照: {m.group(1)}")
    return sorted(set(problems))


def validate_template(t: dict, marts: set[str]) -> list[str]:
    """テンプレート1件の形式検査。問題の一覧を返す。"""
    problems: list[str] = []
    for k in REQUIRED_KEYS:
        if k not in t:
            problems.append(f"キー欠落: {k}")
    if problems:
        return problems
    if not re.fullmatch(r"[a-z][a-z0-9_]*", t["id"]):
        problems.append("id は英小文字・数字・_ のみ")
    if t["mart"] not in marts:
        problems.append(f"未知のマート: {t['mart']}")
    chart = t["chart"]
    if chart.get("type") not in VALID_CHARTS:
        problems.append(f"chart.type が不正: {chart.get('type')}")
    for k in ("x", "y", "series"):
        if k not in chart:
            problems.append(f"chart.{k} が無い")
    names = set()
    for p in t["params"]:
        for k in ("name", "label", "type", "default"):
            if k not in p:
                problems.append(f"param のキー欠落: {k}")
        if p.get("type") not in VALID_TYPES:
            problems.append(f"param.type が不正: {p.get('type')}")
        names.add(p.get("name"))
    used = set(_PLACEHOLDER.findall(t["sql"]))
    if used - names:
        problems.append(f"宣言の無いパラメータ: {sorted(used - names)}")
    if names - used:
        problems.append(f"使われていないパラメータ: {sorted(names - used)}")
    problems += check_dialect(t["sql"], marts)
    return problems


def _render_value(p: dict, raw) -> str:
    kind = p["type"]
    name = p["name"]
    if kind == "string":
        if not isinstance(raw, str):
            raise TemplateError(f"{name}: 文字列で指定してください")
        if not raw or len(raw) > 100 or _BAD_STRING_CHARS.search(raw):
            raise TemplateError(f"{name}: 使えない文字（引用符・バックスラッシュ・;・コメント記号・制御文字）を含むか長さが不正")
        return "'" + raw + "'"
    if isinstance(raw, bool):
        raise TemplateError(f"{name}: 数値で指定してください")
    if kind == "int":
        if isinstance(raw, float) and raw.is_integer():
            raw = int(raw)
        if isinstance(raw, str) and _INT_RE.match(raw.strip()):
            raw = int(raw.strip())
        if not isinstance(raw, int):
            raise TemplateError(f"{name}: 整数で指定してください")
        value = raw
    else:
        if isinstance(raw, str):
            try:
                raw = float(raw)
            except ValueError:
                raise TemplateError(f"{name}: 数値で指定してください") from None
        if not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise TemplateError(f"{name}: 有限の数値で指定してください")
        value = raw
    if "min" in p and value < p["min"]:
        raise TemplateError(f"{name}: {p['min']} 以上にしてください")
    if "max" in p and value > p["max"]:
        raise TemplateError(f"{name}: {p['max']} 以下にしてください")
    return repr(value)


def render_sql(t: dict, values: dict | None = None) -> str:
    """{{name}} を型検査したうえで置換する。未指定は default。"""
    values = dict(values or {})
    decl = {p["name"]: p for p in t["params"]}
    unknown = set(values) - set(decl)
    if unknown:
        raise TemplateError(f"未知のパラメータ: {sorted(unknown)}")
    rendered = {n: _render_value(p, values.get(n, p["default"])) for n, p in decl.items()}
    return _PLACEHOLDER.sub(lambda m: rendered[m.group(1)], t["sql"])


def compose_bigquery_sql(sql: str, mart_sqls: dict[str, str], used_marts: list[str]) -> str:
    """Claudeダッシュボード用：マートSQLをCTEにして、テンプレSQLの前に付ける。
    テンプレ自身が WITH で始まる場合は CTE を連結する（WITH を2回書くと構文エラーになるため）。"""
    ctes = ",\n".join(f"{m} AS (\n{mart_sqls[m].strip()}\n)" for m in used_marts)
    body = sql.strip()
    if re.match(r"(?i)^WITH\b", body):
        return f"WITH {ctes},\n{body[4:].lstrip()}"
    return f"WITH {ctes}\n{body}"
