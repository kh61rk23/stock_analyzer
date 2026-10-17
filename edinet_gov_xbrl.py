# ==============================================================
# edinet_gov_xbrl.py
# 政府 EDINET API v2 から有報の XBRL を取り、IFRS 会社の投資不動産（公正価値の注記を含む）の要素を調べる
# ==============================================================
# 【なぜ】
#   賃貸等不動産の時価は EdinetDB real-estate で取れるが、IFRS 会社の「投資不動産の公正価値」は
#   EdinetDB が抽出していない（10/17 に IFRS 5社で確認：時価の付いた年度なし）。
#   政府 EDINET API（無料・キー登録制）の XBRL なら、注記の要素をそのまま読める。
# 【いまの段階】
#   要素名・単位・期間の持ち方を推測で決めないため、まず --probe で IFRS 会社の有報1件から
#   要素名に InvestmentProperty を含む要素を全部書き出し、それを見てから取り込みを書く。
# 【キー】GitHub Secret EDINET_GOV_API_KEY（健太が登録して設定する）。無ければ何もしない。
# 【実行】python3 edinet_gov_xbrl.py --probe E03516 [E02513 ...]（manual_task edinet_gov_probe）
# ==============================================================
from __future__ import annotations

import argparse
import html as _html
import io
import json
import os
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

API = "https://api.edinet-fsa.go.jp/api/v2"
MATCH = re.compile(r"InvestmentProperty|InvestmentProperties|RealEstateForLease", re.I)

DOC_SQL = """
SELECT doc_id, fiscal_year_end, disclosure_date
FROM `{project}.edinet.doc_index`
WHERE edinet_code = @code AND doc_type = '有価証券報告書'
ORDER BY disclosure_date DESC LIMIT 1
"""


def facts_from_xbrl(xml: str) -> list[dict]:
    """XBRL インスタンスから、要素名が MATCH に当たる要素を取り出す（純関数）。値は先頭600字。"""
    out = []
    # 事実の要素は子要素を持たない（テキストブロックの HTML はエスケープされている）ので、中身は < を含まない
    pat = re.compile(r"<([A-Za-z0-9_\-]+):([A-Za-z0-9_]+)\b([^>]*)>([^<]*)</\1:\2>", re.S)
    for m in pat.finditer(xml or ""):
        prefix, name, attrs, val = m.groups()
        if not MATCH.search(name):
            continue
        a = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        text = re.sub(r"<[^>]+>", " ", _html.unescape(val))
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"element": f"{prefix}:{name}", "contextRef": a.get("contextRef"), "unitRef": a.get("unitRef"),
                    "decimals": a.get("decimals"), "value": text[:600], "is_text_block": name.endswith("TextBlock")})
    return out


def _instance_from_zip(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.endswith(".xbrl") and "PublicDoc" in n] or \
                [n for n in z.namelist() if n.endswith(".xbrl")]
        if not names:
            return ""
        raw = z.read(sorted(names, key=len)[0])
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def probe(cfg, codes: list[str]) -> int:
    key = os.environ.get("EDINET_GOV_API_KEY", "").strip()
    if not key:
        print("⏩ EDINET_GOV_API_KEY が無い（健太が政府 EDINET API のキーを登録して Secret に入れると動く）")
        return 0
    import requests
    from google.cloud import bigquery
    from edinet_bq_client import EDINETBQClient
    client = EDINETBQClient().client
    out_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    rc = 0
    for code in codes:
        rows = list(client.query(DOC_SQL.format(project=client.project), job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("code", "STRING", code)])).result())
        if not rows:
            print(f"❌ {code}: doc_index に有報が無い")
            rc = 1
            continue
        doc = rows[0]
        r = requests.get(f"{API}/documents/{doc.doc_id}", params={"type": 1, "Subscription-Key": key}, timeout=120)
        if r.status_code != 200 or "zip" not in r.headers.get("Content-Type", ""):
            print(f"❌ {code} {doc.doc_id}: HTTP {r.status_code} {r.headers.get('Content-Type')} {r.text[:200]}")
            rc = 1
            continue
        facts = facts_from_xbrl(_instance_from_zip(r.content))
        p = out_dir / f"edinet_gov_probe_{code}.json"
        p.write_text(json.dumps({"fetched": date.today().isoformat(), "edinet_code": code, "doc_id": doc.doc_id,
                                 "fiscal_year_end": str(doc.fiscal_year_end), "facts": facts},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        names = sorted({f["element"] for f in facts})
        print(f"✅ {code} {doc.doc_id}: 投資不動産の要素 {len(facts)}件（{len(names)}種類）→ {p.name}")
        for n in names[:30]:
            print(f"    {n}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", nargs="+", metavar="EDINET_CODE", required=True)
    args = ap.parse_args(argv)
    import config
    return probe(config, args.probe)


if __name__ == "__main__":
    sys.exit(main())
