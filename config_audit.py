# ==============================================================
# config_audit.py  v1.0  (Wave SSOT-2)
# config.py 単一真実源（SSOT）整合性監査ツール
# ==============================================================
# 【役割】
#   config.py と参照側モジュールの整合性を静的解析で検査し、
#   「黙って壊れる」クラスの不具合を AI/人間のレビュー前に検出する。
#
# 【検査項目】
#   A. config.py モジュールレベルの重複定義（後勝ち上書き）  → CRITICAL
#      ※ 2026-06 に LEADING_INDICATOR_RULES の三重定義で
#        先行指標ライブラリ7本が無効化されていた実害事例あり。
#   B. BLOCK ヘッダーコメントの番号衝突                       → WARNING
#   C. APIキー・トークン類のハードコード（環境変数化漏れ）    → CRITICAL
#   D. 各モジュールの cfg.X 参照のうち config に存在しない属性 → WARNING
#      ※ run_headless.py の ENV_MAP 注入分はホワイトリスト扱い。
#
# 【実行方法】
#   python3 config_audit.py            # 人間向けレポートを標準出力
#   python3 config_audit.py --md out.md  # Markdownレポートも出力
#
# 【終了コード】
#   0: CRITICAL なし / 1: CRITICAL あり（CIゲートとして利用可）
#
# 【設計思想】
#   標準ライブラリのみ・外部依存ゼロ・config の import 副作用なし
#   （AST解析を基本とし、namespace取得のみ実 import を使用）
# ==============================================================

from __future__ import annotations

import ast
import collections
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent
CONFIG_PATH = REPO_ROOT / "config.py"

# 値がこのパターンを含む変数名はシークレット扱い
SECRET_NAME_PAT = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)
# os.getenv / os.environ 経由なら安全とみなす
SAFE_VALUE_PAT = re.compile(r"os\.(getenv|environ)")
# プレースホルダー的な値は除外
PLACEHOLDER_PAT = re.compile(r"^(|YOUR_.*|<.*>|xxx+|sk-xxx.*|dummy.*|test.*)$", re.IGNORECASE)

# run_headless.py の ENV_MAP で実行時注入される属性（check Dのホワイトリスト）
def _load_env_injected_attrs() -> set:
    attrs = set()
    rh = REPO_ROOT / "run_headless.py"
    if not rh.exists():
        return attrs
    try:
        tree = ast.parse(rh.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "ENV_MAP" and isinstance(node.value, ast.Dict):
                        for k in node.value.keys:
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                attrs.add(k.value)
    except Exception:
        pass
    return attrs


def check_a_duplicates(tree: ast.Module) -> list:
    """A: モジュールレベル重複定義の検出"""
    assigns = collections.defaultdict(list)
    for node in tree.body:
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            assigns[n].append(node.lineno)
    return [
        {"name": k, "lines": v}
        for k, v in sorted(assigns.items(), key=lambda x: x[1][0])
        if len(v) > 1
    ]


def check_b_block_collisions(src: str) -> list:
    """B: BLOCKヘッダー番号の衝突検出"""
    blocks = collections.defaultdict(list)
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"#\s*BLOCK\s+([\w.\-]+)\s*:", line)
        if m:
            blocks[m.group(1)].append(i)
    return [
        {"block": k, "lines": v}
        for k, v in sorted(blocks.items())
        if len(v) > 1
    ]


def check_c_hardcoded_secrets(tree: ast.Module, src: str) -> list:
    """C: シークレットのハードコード検出"""
    findings = []
    src_lines = src.splitlines()
    for node in tree.body:
        names, value = [], None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            value = node.value
        for n in names:
            if not SECRET_NAME_PAT.search(n):
                continue
            if value is None:
                continue
            line_src = src_lines[node.lineno - 1] if node.lineno <= len(src_lines) else ""
            if SAFE_VALUE_PAT.search(line_src):
                continue  # 環境変数経由 → OK
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                v = value.value
                if PLACEHOLDER_PAT.match(v) or len(v) < 12:
                    continue  # 空文字・プレースホルダー → OK
                findings.append({"name": n, "line": node.lineno,
                                 "preview": v[:8] + "..."})
    return findings


def check_d_missing_cfg_attrs(cfg_names: set, env_injected: set) -> list:
    """D: cfg.X 参照のうち config 名前空間に存在しない属性の検出"""
    findings = []
    for py in sorted(REPO_ROOT.glob("*.py")):
        if py.name in ("config.py", "config_audit.py"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        missing = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("cfg", "config")
                    and isinstance(node.ctx, ast.Load)
                    and node.attr.isupper()):
                if node.attr not in cfg_names and node.attr not in env_injected:
                    missing.add(node.attr)
        if missing:
            findings.append({"file": py.name, "attrs": sorted(missing)})
    return findings


# E: 未参照でも正当な定数（理由を必ず添えること）
# ここに足す前に「本当に誰も読まないのか」を確認する。安易に足すと検査が死ぬ。
UNREFERENCED_ALLOWLIST = {
    # 旧名を残すための明示エイリアス。実体は ENABLE_FUDOSAN_INGEST 側が読まれる。
    "ENABLE_FUDOSANDB_INGEST",
    # ↓ 実装が存在せずフラグだけ先行しているもの。
    #   config.py 側に「未実装」と明記済み。実装したら本リストから外すこと。
    "ENABLE_TREASURY_DATA",      # treasury_debt の取得処理が無い（読み手だけ）
    "ENABLE_GEO_SCORE_PERSIST",  # analytics.company_scores への書込処理が無い
    # 退役機能の墓標。legacy 業種別REST取得は廃止済みで実装も無い。False 固定であることを
    # tests/test_db_integration_refit.py が保証している（復活防止）。
    "ENABLE_EDINET",
    # ↓ 消費者が .py ではないため検査Eの走査に掛からないもの
    #   Streamlit のポートは起動時 --server.port で渡すしかないため、
    #   setup/権益レビューUI起動.command が唯一の消費者。
    "EXPOSURE_REVIEW_UI_PORT",
    # ↓ 実装が存在しないフラグ（config 側に「未実装」と明記済み）
    "GARCH_ENABLED",                # GARCH ボラ推定は未実装（ARCH未導入）
    "IGNITION_CATEGORY_D_ENABLED",  # ignition_detector にカテゴリD抽出が無い
}

# 走査から外すディレクトリ
# tests/ を外すのは、テストからの参照で「本番で使われている」ように
# 見えてしまうのを防ぐため。テストだけが読む定数は
# 本番未使用であり、検査Eはそれを検出できなければ意味がない。
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "pytest_cache",
              "03_bk", "tests"}


def _iter_py_files():
    """リポジトリ配下の全 .py（サブディレクトリ含む）。"""
    for p in sorted(REPO_ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


def check_e_unreferenced_constants(tree: ast.Module, src: str) -> list:
    """E: config.py で定義されたが、どこからも読まれていない定数の検出。

    【なぜ要るか】config.py の定数は実装から一度も参照されないまま残りやすい。
    とくに ENABLE_* は「True にしたのに動かない」「False にしたのに止まらない」を生む。
    例: ENABLE_MIDTERM_TRACKER と ENABLE_MIDTERM_PLAN_TRACKER が同居し、
    実装が後者だけを読むと前者を変えても効かない（二重フラグの罠）。

    【検出方法】定数名を単語境界で全 .py から探す。文字列リテラルも拾うので
    getattr(cfg, "NAME") 形式も参照済みとみなす。
    【限界】getattr(cfg, f"ENABLE_{x}") のような動的組み立ては追えない。
    その形式を使う場合は UNREFERENCED_ALLOWLIST に登録すること。
    """
    consts = []
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and target.id.isupper():
            consts.append((target.id, node.lineno))

    # config.py 以外の全ソースを連結して1回だけ検索する（定数×ファイル数の
    # 総当たりだと数十秒かかるため）
    blob_parts = []
    for p in _iter_py_files():
        if p.name in ("config.py", "config_audit.py"):
            continue
        try:
            blob_parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
    blob = "\n".join(blob_parts)

    findings = []
    for name, lineno in consts:
        if name in UNREFERENCED_ALLOWLIST:
            continue
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        if pat.search(blob):
            continue
        # config.py 内での自己参照（他の定数の計算に使う等）があれば生きている
        if len(pat.findall(src)) > 1:
            continue
        findings.append({"name": name, "line": lineno,
                         "is_flag": name.startswith("ENABLE_")})
    # 挙動フラグを先頭に寄せる（読み手が最初に見るべきもの）
    findings.sort(key=lambda f: (not f["is_flag"], f["line"]))
    return findings


def main() -> int:
    src = CONFIG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # config 名前空間（getattrデフォルト検査用）
    import importlib.util
    spec = importlib.util.spec_from_file_location("config", str(CONFIG_PATH))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    cfg_names = {k for k in dir(cfg) if not k.startswith("_")}
    env_injected = _load_env_injected_attrs()

    a = check_a_duplicates(tree)
    b = check_b_block_collisions(src)
    c = check_c_hardcoded_secrets(tree, src)
    d = check_d_missing_cfg_attrs(cfg_names, env_injected)
    e = check_e_unreferenced_constants(tree, src)

    critical = bool(a or c)
    lines = []
    lines.append("=" * 60)
    lines.append("config_audit.py — SSOT整合性監査レポート")
    lines.append("=" * 60)

    lines.append(f"\n[A] 重複定義（CRITICAL）: {len(a)}件")
    for f in a:
        lines.append(f"  ❌ {f['name']}: lines {f['lines']}（後勝ち上書き発生）")

    lines.append(f"\n[B] BLOCK番号衝突（WARNING）: {len(b)}件")
    for f in b:
        lines.append(f"  ⚠️  BLOCK {f['block']}: lines {f['lines']}")

    lines.append(f"\n[C] シークレットのハードコード（CRITICAL）: {len(c)}件")
    for f in c:
        lines.append(f"  ❌ {f['name']} (L{f['line']}): {f['preview']} → 環境変数化が必要")

    lines.append(f"\n[D] 存在しない cfg 属性参照（WARNING）: {len(d)}ファイル")
    for f in d:
        lines.append(f"  ⚠️  {f['file']}: {', '.join(f['attrs'][:8])}"
                     + (" ..." if len(f["attrs"]) > 8 else ""))

    e_flags = [f for f in e if f["is_flag"]]
    lines.append(f"\n[E] 未参照の config 定数（WARNING）: {len(e)}件"
                 f"（うち ENABLE_* フラグ {len(e_flags)}件）")
    if e_flags:
        lines.append("  ── 挙動フラグ（切り替えても効かない＝実害が出やすい）──")
        for f in e_flags:
            lines.append(f"  ⚠️  L{f['line']}: {f['name']}")
    e_others = [f for f in e if not f["is_flag"]]
    if e_others:
        lines.append(f"  ── 閾値・パラメータ {len(e_others)}件 ──")
        for f in e_others:
            lines.append(f"      L{f['line']}: {f['name']}")

    lines.append("\n" + "=" * 60)
    lines.append("✅ CRITICAL なし" if not critical
                 else "❌ CRITICAL あり — 修正が必要")
    report = "\n".join(lines)
    print(report)

    if "--md" in sys.argv:
        idx = sys.argv.index("--md")
        out = Path(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else Path("config_audit_report.md")
        out.write_text("```\n" + report + "\n```\n", encoding="utf-8")
        print(f"\nMarkdownレポート出力: {out}")

    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
