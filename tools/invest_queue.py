#!/usr/bin/env python3
"""投資審議キュー CLI（クラウド routine・Cowork から BigQuery の invest.* を操作する）。

  claim   --limit N            pending の上位 N 件を claimed にし、パケット込みの JSON を標準出力へ
  complete --case ID --dir D   作業ディレクトリの bull/bear/devil/report.md と結論 YAML を
                               invest.deliberations に登録し、キューを done に、実行記録を更新する
  release [--case ID] [--older-than-hours H] [--error 理由]
                               claimed を pending に戻す（失敗時の後始末。--error で実行記録に失敗を残す）
  due     [--interval-days N]  前回の成功した実行から N 日（既定14）以上か判定する
  ledger  [--limit N]          直近の審議一覧（判断台帳の代わり。デビル・ベアが過去の判断を読む用）

認証: edinet_bq_client と同じ。BQ_CREDENTIALS（JSON文字列）または BQ_CREDENTIALS_PATH。
承認欄（approval_status / approved_by / approved_at / review_on）は書かない。人間だけが更新する。
作業ディレクトリのファイル:
  bull.md / bear.md / devil.md / report.md  必須
  packet.md                                 任意（無ければキューの packet_md を使う）
  result.yaml                               任意（verdict・conviction・layer・kpi・models などの上書き）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from invest_bq_schema import DATASET, DELIB_TABLE, LOCATION, QUEUE_TABLE, RUNS_TABLE  # noqa: E402

VERDICTS = ("Buy", "Watch", "Avoid")
REQUIRED_FILES = ("bull.md", "bear.md", "devil.md", "report.md")
JST = timezone(timedelta(hours=9))


# ── 純粋ロジック（pytest 対象） ────────────────────────────────

def case_id_for(ticker: str, today: date) -> str:
    """case_id は YYYY-MM-DD_<tickerのドット除去>。"""
    return f"{today.isoformat()}_{ticker.replace('.', '')}"


def parse_frontmatter(text: str) -> dict:
    """先頭の --- ... --- を YAML として読む。無い・壊れていれば空 dict。"""
    import yaml
    m = re.match(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", text or "", re.S)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def extract_yaml_blocks(text: str) -> list:
    """本文中の ```yaml フェンスを順に dict として返す（壊れたブロックは飛ばす）。"""
    import yaml
    out = []
    for m in re.finditer(r"```ya?ml[^\n]*\n(.*?)```", text or "", re.S):
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def memo_conclusion(text: str, agent: str) -> dict:
    """メモ末尾の共通 YAML（agent: bull|bear|devil）を返す。複数あれば agent が合う最後のもの。"""
    hit: dict = {}
    for b in extract_yaml_blocks(text):
        if str(b.get("agent", "")).strip() == agent:
            hit = b
    return hit


def _int(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def count_reversal(devil_md: str) -> int:
    """デビルの「未解消の重大指摘」節にある【反転級】の件数。節が無ければ 0。"""
    m = re.search(r"^#{1,4}\s*未解消の重大指摘[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)", devil_md or "", re.S | re.M)
    if not m:
        return 0
    return len(re.findall(r"【反転級】", m.group(1)))


KPI_ROW = re.compile(r"^\|\s*([①②③④⑤⑥⑦⑧⑨])\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|", re.M)


def parse_kpi(report_md: str) -> dict:
    """レポートの §Ⓚ 審議KPI 表（①〜⑨）を {"① 項目名": 値} にする。表が無ければ空。"""
    return {f"{n} {name}": val for n, name, val in KPI_ROW.findall(report_md or "")}


def parse_deliberation(files: dict, queue_row: dict | None = None, run_env: str = "cloud_routine",
                       overrides: dict | None = None, now: datetime | None = None) -> dict:
    """作業ディレクトリの中身（{"bull.md": 本文, ...}）から deliberations の行を組む。

    verdict / conviction は report.md の frontmatter を正本とし、無ければ result.yaml、
    それも無ければエラー（空の結論を DB に入れない）。承認列は常に空。
    """
    missing = [f for f in REQUIRED_FILES if not (files.get(f) or "").strip()]
    if missing:
        raise ValueError(f"作業ディレクトリに必須ファイルが無い／空: {', '.join(missing)}")
    ov = overrides or {}
    q = queue_row or {}
    fm = parse_frontmatter(files["report.md"])
    bull = memo_conclusion(files["bull.md"], "bull")
    bear = memo_conclusion(files["bear.md"], "bear")
    devil = memo_conclusion(files["devil.md"], "devil")

    def pick(key: str, *alts):
        for src in (ov, fm, *alts):
            if src.get(key) not in (None, ""):
                return src[key]
        return None

    verdict = str(pick("verdict") or "").strip()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict が {VERDICTS} のいずれでもない: {verdict!r}（report.md の frontmatter か result.yaml）")
    conviction = _int(pick("conviction"))
    if conviction is None or not 1 <= conviction <= 5:
        raise ValueError(f"conviction が 1〜5 でない: {pick('conviction')!r}")

    case_id = str(pick("case_id") or q.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id が決まらない")
    ticker = str(pick("ticker") or q.get("ticker") or "").strip() or None
    return {
        "case_id": case_id,
        "ticker": ticker,
        "company_name": pick("company_name") or pick("company") or q.get("company_name"),
        "sector_code": str(pick("sector_code") or q.get("sector_code") or "") or None,
        "deliberated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "verdict": verdict,
        "conviction": conviction,
        "layer": pick("layer"),
        "bull_conviction": _int(ov.get("bull_conviction")) or _int(fm.get("bull_conviction")) or _int(bull.get("conviction")),
        "bear_conviction": _int(ov.get("bear_conviction")) or _int(fm.get("bear_conviction")) or _int(bear.get("conviction")),
        "devil_unresolved": _first_int(ov.get("devil_unresolved"), fm.get("unresolved"),
                                       devil.get("unresolved_critical")),
        "devil_reversal": _first_int(ov.get("devil_reversal"), count_reversal(files["devil.md"])),
        "kpi": ov.get("kpi") or parse_kpi(files["report.md"]) or None,
        "bull_memo": files["bull.md"],
        "bear_memo": files["bear.md"],
        "devil_memo": files["devil.md"],
        "report_md": files["report.md"],
        "packet_md": files["packet.md"] if (files.get("packet.md") or "").strip() else q.get("packet_md"),
        "models": ov.get("models") or None,
        "run_env": str(ov.get("run_env") or run_env),
        "approval_status": None,
        "approved_by": None,
        "approved_at": None,
        "review_on": None,
    }


def _first_int(*vals):
    for v in vals:
        n = _int(v)
        if n is not None:
            return n
    return None


def due_decision(last_success: datetime | None, now: datetime, interval_days: int = 14) -> tuple:
    """前回の成功した実行から interval_days 日以上なら (True, 理由)。記録が無ければ初回で True。"""
    if last_success is None:
        return True, "実行記録なし（初回）"
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=timezone.utc)
    days = (now - last_success).days
    if days >= interval_days:
        return True, f"前回から{days}日（間隔{interval_days}日）"
    return False, f"前回から{days}日（間隔{interval_days}日に未達）"


def read_work_dir(d: Path) -> tuple:
    """作業ディレクトリ → ({ファイル名: 本文}, result.yaml の dict)。"""
    import yaml
    files = {}
    for name in (*REQUIRED_FILES, "packet.md"):
        p = d / name
        if p.exists():
            files[name] = p.read_text(encoding="utf-8")
    ov = {}
    ry = d / "result.yaml"
    if ry.exists():
        data = yaml.safe_load(ry.read_text(encoding="utf-8"))
        ov = data if isinstance(data, dict) else {}
    return files, ov


# ── BQ I/O ────────────────────────────────────────────────────

def _client():
    from edinet_bq_client import EDINETBQClient
    return EDINETBQClient().client


def _fq(client, name: str) -> str:
    return f"`{client.project}.{DATASET}.{name}`"


def _q(client, sql: str, params: list | None = None):
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return client.query(sql, job_config=cfg, location=LOCATION)


def _p(name, typ, val):
    from google.cloud import bigquery
    return bigquery.ScalarQueryParameter(name, typ, val)


def _arr(name, vals):
    from google.cloud import bigquery
    return bigquery.ArrayQueryParameter(name, "STRING", vals)


def claim(client, limit: int, today: date | None = None, run_env: str = "cloud_routine") -> dict:
    today = today or datetime.now(JST).date()
    rows = list(_q(client, f"""
        SELECT queue_date, ticker, company_name, sector_code, source, reason, priority_rank, packet_md
        FROM {_fq(client, QUEUE_TABLE)}
        WHERE queue_date = (SELECT MAX(queue_date) FROM {_fq(client, QUEUE_TABLE)})
          AND status = 'pending'
        ORDER BY priority_rank LIMIT @n""", [_p("n", "INT64", int(limit))]).result())
    run_id = uuid.uuid4().hex[:12]
    if not rows:
        return {"run_id": None, "claimed": [], "message": "pending のキューがない"}
    qd = rows[0]["queue_date"]
    tickers = [r["ticker"] for r in rows]
    _q(client, f"""
        UPDATE {_fq(client, QUEUE_TABLE)}
        SET status = 'claimed', claimed_at = CURRENT_TIMESTAMP(), updated_at = CURRENT_TIMESTAMP(),
            case_id = CONCAT(@d, '_', REPLACE(ticker, '.', ''))
        WHERE queue_date = @qd AND ticker IN UNNEST(@t) AND status = 'pending'""",
       [_p("d", "STRING", today.isoformat()), _p("qd", "DATE", qd),
        _arr("t", tickers)]).result()
    claimed = []
    for r in rows:
        cid = case_id_for(r["ticker"], today)
        claimed.append({"case_id": cid, "ticker": r["ticker"], "company_name": r["company_name"],
                        "sector_code": r["sector_code"], "source": r["source"], "reason": r["reason"],
                        "priority_rank": r["priority_rank"], "packet_md": r["packet_md"]})
    _q(client, f"""
        INSERT INTO {_fq(client, RUNS_TABLE)}
          (run_id, started_at, run_env, status, claimed_count, completed_count, case_ids)
        VALUES (@r, CURRENT_TIMESTAMP(), @e, 'running', @n, 0, @c)""",
       [_p("r", "STRING", run_id), _p("e", "STRING", run_env), _p("n", "INT64", len(claimed)),
        _p("c", "STRING", ",".join(x["case_id"] for x in claimed))]).result()
    return {"run_id": run_id, "claimed": claimed}


def complete(client, case_id: str, work_dir: Path, run_id: str | None = None,
             run_env: str = "cloud_routine", replace: bool = False) -> dict:
    files, ov = read_work_dir(work_dir)
    qrows = list(_q(client, f"""
        SELECT ticker, company_name, sector_code, case_id, packet_md
        FROM {_fq(client, QUEUE_TABLE)} WHERE case_id = @c LIMIT 1""", [_p("c", "STRING", case_id)]).result())
    qrow = dict(qrows[0].items()) if qrows else {"case_id": case_id}
    qrow["case_id"] = case_id
    row = parse_deliberation(files, qrow, run_env=run_env, overrides=ov)

    exists = list(_q(client, f"SELECT 1 FROM {_fq(client, DELIB_TABLE)} WHERE case_id = @c LIMIT 1",
                     [_p("c", "STRING", case_id)]).result())
    if exists and not replace:
        raise RuntimeError(f"case_id {case_id} は既に登録済み（上書きするなら --replace）")
    if exists:
        _q(client, f"DELETE FROM {_fq(client, DELIB_TABLE)} WHERE case_id = @c",
           [_p("c", "STRING", case_id)]).result()
    _insert_deliberation(client, row)
    _q(client, f"""
        UPDATE {_fq(client, QUEUE_TABLE)} SET status = 'done', updated_at = CURRENT_TIMESTAMP()
        WHERE case_id = @c""", [_p("c", "STRING", case_id)]).result()
    status = _bump_run(client, run_id)
    return {"case_id": case_id, "verdict": row["verdict"], "conviction": row["conviction"],
            "layer": row["layer"], "run_status": status}


def _insert_deliberation(client, r: dict) -> None:
    """全列を query parameter で渡す（本文に引用符・改行が入るため文字列連結しない）。JSON 列は PARSE_JSON。"""
    cols = ["case_id", "ticker", "company_name", "sector_code", "deliberated_at", "verdict", "conviction",
            "layer", "bull_conviction", "bear_conviction", "devil_unresolved", "devil_reversal", "kpi",
            "bull_memo", "bear_memo", "devil_memo", "report_md", "packet_md", "models", "run_env",
            "approval_status", "approved_by", "approved_at", "review_on"]
    types = {"deliberated_at": "TIMESTAMP", "approved_at": "TIMESTAMP", "review_on": "DATE",
             "conviction": "INT64", "bull_conviction": "INT64", "bear_conviction": "INT64",
             "devil_unresolved": "INT64", "devil_reversal": "INT64"}
    params, exprs = [], []
    for c in cols:
        v = r.get(c)
        if c in ("kpi", "models"):
            params.append(_p(c, "STRING", None if v is None else json.dumps(v, ensure_ascii=False)))
            exprs.append(f"PARSE_JSON(@{c})")
            continue
        t = types.get(c, "STRING")
        if t == "TIMESTAMP" and isinstance(v, str):
            v = datetime.fromisoformat(v)
        if t == "DATE" and isinstance(v, str):
            v = date.fromisoformat(v)
        params.append(_p(c, t, v))
        exprs.append(f"@{c}")
    _q(client, f"INSERT INTO {_fq(client, DELIB_TABLE)} ({', '.join(cols)}) VALUES ({', '.join(exprs)})",
       params).result()


def _bump_run(client, run_id: str | None) -> str | None:
    if run_id is None:
        rows = list(_q(client, f"SELECT run_id FROM {_fq(client, RUNS_TABLE)} WHERE status = 'running' "
                               f"ORDER BY started_at DESC LIMIT 1").result())
        if not rows:
            return None
        run_id = rows[0]["run_id"]
    _q(client, f"""
        UPDATE {_fq(client, RUNS_TABLE)}
        SET completed_count = completed_count + 1, finished_at = CURRENT_TIMESTAMP(),
            status = IF(completed_count + 1 >= claimed_count, 'ok', 'running')
        WHERE run_id = @r""", [_p("r", "STRING", run_id)]).result()
    got = list(_q(client, f"SELECT status FROM {_fq(client, RUNS_TABLE)} WHERE run_id = @r",
                  [_p("r", "STRING", run_id)]).result())
    return got[0]["status"] if got else None


def release(client, case_id: str | None = None, older_than_hours: float | None = None,
            error: str | None = None, run_id: str | None = None) -> dict:
    """claimed を pending に戻す。--error があれば実行記録を failed にして理由を残す。"""
    where = ["status = 'claimed'"]
    params: list = []
    if case_id:
        where.append("case_id = @c")
        params.append(_p("c", "STRING", case_id))
    if older_than_hours is not None:
        where.append("claimed_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @m MINUTE)")
        params.append(_p("m", "INT64", int(older_than_hours * 60)))
    job = _q(client, f"""
        UPDATE {_fq(client, QUEUE_TABLE)}
        SET status = 'pending', claimed_at = NULL, case_id = NULL, updated_at = CURRENT_TIMESTAMP()
        WHERE {' AND '.join(where)}""", params)
    job.result()
    n = job.num_dml_affected_rows or 0
    if error:
        if run_id:
            sql, p = (f"UPDATE {_fq(client, RUNS_TABLE)} SET status='failed', finished_at=CURRENT_TIMESTAMP(), "
                      f"error=@e WHERE run_id=@r", [_p("e", "STRING", error[:500]), _p("r", "STRING", run_id)])
        else:
            sql, p = (f"UPDATE {_fq(client, RUNS_TABLE)} SET status='failed', finished_at=CURRENT_TIMESTAMP(), "
                      f"error=@e WHERE status='running'", [_p("e", "STRING", error[:500])])
        _q(client, sql, p).result()
    return {"released": n, "error_recorded": bool(error)}


def due(client, interval_days: int = 14, now: datetime | None = None) -> dict:
    rows = list(_q(client, f"""
        SELECT started_at FROM {_fq(client, RUNS_TABLE)}
        WHERE status = 'ok' AND completed_count > 0 ORDER BY started_at DESC LIMIT 1""").result())
    last = rows[0]["started_at"] if rows else None
    ok, why = due_decision(last, now or datetime.now(timezone.utc), interval_days)
    return {"due": ok, "reason": why, "last_success": last.isoformat() if last else None}


def ledger(client, limit: int = 30) -> dict:
    rows = _q(client, f"""
        SELECT case_id, ticker, company_name, verdict, conviction, layer, devil_unresolved,
               approval_status, DATE(approved_at, 'Asia/Tokyo') AS approved_on
        FROM {_fq(client, DELIB_TABLE)} ORDER BY deliberated_at DESC LIMIT @n""",
              [_p("n", "INT64", int(limit))]).result()
    return {"deliberations": [dict(r.items()) for r in rows]}


# ── CLI ───────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--limit", type=int, default=3)
    c.add_argument("--run-env", default="cloud_routine")
    k = sub.add_parser("complete")
    k.add_argument("--case", required=True)
    k.add_argument("--dir", required=True)
    k.add_argument("--run-id")
    k.add_argument("--run-env", default="cloud_routine")
    k.add_argument("--replace", action="store_true")
    r = sub.add_parser("release")
    r.add_argument("--case")
    r.add_argument("--older-than-hours", type=float)
    r.add_argument("--error")
    r.add_argument("--run-id")
    d = sub.add_parser("due")
    d.add_argument("--interval-days", type=int, default=14)
    lg = sub.add_parser("ledger")
    lg.add_argument("--limit", type=int, default=30)
    args = ap.parse_args(argv)

    try:
        client = _client()
        if args.cmd == "claim":
            out = claim(client, args.limit, run_env=args.run_env)
        elif args.cmd == "complete":
            out = complete(client, args.case, Path(args.dir), args.run_id, args.run_env, args.replace)
        elif args.cmd == "release":
            out = release(client, args.case, args.older_than_hours, args.error, args.run_id)
        elif args.cmd == "ledger":
            out = ledger(client, args.limit)
        else:
            out = due(client, args.interval_days)
    except Exception as e:
        print(json.dumps({"error": str(e)[:500]}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
