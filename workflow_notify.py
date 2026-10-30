# ==============================================================
# workflow_notify.py — 取得系の workflow が終わったら、取り込んだ内容をメールで知らせる
# ==============================================================
# 使い方（workflow の最後のステップ。if: always()）:
#   python workflow_notify.py --workflow fetch_jp_filings --title "日本企業の開示データ取得" \
#       --tables edinet.gov_documents,edinet.gov_doc_facts
# 中身:
#   ・各ステップの成否・秒数・✅⚠️❌の数と要点の行（actions_log.py が logs/step_results_<workflow>_<実行ID>.jsonl に残したもの）
#   ・--tables の表の行数と、前回この workflow が知らせた時からの増減（表のメタデータを読むだけ＝課金されない）
#   ・「今回取ったもの」（DIGESTS。取り込みが結果に残した日付の範囲・この回の開始時刻で BQ を引く。1つ数本のクエリ）
#   ・BigQuery の保存量（logs/bq_storage.json があれば）と、実行ページへのリンク
# 送り先は日次メールと同じ（ALERT_GMAIL_FROM / ALERT_GMAIL_PASSWORD / ALERT_GMAIL_TO）。WORKFLOW_NOTIFY_ENABLED=False で止める。
# メールの中身は Actions のログに出さない（public リポジトリのため）。ログには送ったかどうかだけを出す。
# 前回の行数は stock_data/data/workflow_notify_counts.json（状態として BigQuery に保存される）。
# ==============================================================
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import actions_log


def read_steps(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def table_counts(client, tables: list[str]) -> dict:
    out = {}
    for t in tables:
        try:
            x = client.get_table(f"{client.project}.{t}")
            out[t] = {"rows": int(x.num_rows or 0), "mb": round((x.num_bytes or 0) / 1e6, 2)}
        except Exception as e:
            out[t] = {"error": type(e).__name__}
    return out


def deltas(now: dict, before: dict) -> dict:
    """{表: 行数の増減}（前回が無い・読めない表は None）。"""
    out = {}
    for t, v in now.items():
        b = (before or {}).get(t) or {}
        out[t] = (v["rows"] - b["rows"]) if "rows" in v and "rows" in b else None
    return out


DOC_TYPES = {"120": "有報", "130": "訂正有報", "135": "確認書", "140": "四半期", "160": "半期", "180": "臨時", "190": "訂正臨時",
             "030": "有価証券届出", "350": "大量保有", "360": "変更報告", "370": "訂正（大量保有）", "220": "自己株券買付",
             "240": "公開買付届出", "270": "公開買付報告", "290": "意見表明", "235": "内部統制"}
ITEMS_8K = {"1.01": "重要な契約", "1.02": "契約の終了", "1.03": "破産", "2.01": "買収・売却の完了", "2.02": "決算",
            "2.03": "債務の発生", "2.05": "撤退・リストラ", "2.06": "減損", "3.01": "上場廃止の通知", "3.02": "未登録の株式発行",
            "4.01": "監査人の交代", "4.02": "過去の決算の訂正", "5.01": "支配権の変更", "5.02": "役員の異動",
            "5.03": "定款の変更", "5.07": "株主総会の結果", "7.01": "FD 開示", "8.01": "その他", "9.01": "財務諸表・添付"}


def step_json(steps: list[dict], step: str) -> dict:
    """そのステップの要点の行「✅ <step>: {...}」の JSON（取り込みが返した結果）。無ければ {}。"""
    for s in steps:
        for h in s.get("highlights") or []:
            if h.lstrip("✅⏩ ").startswith(f"{step}: {{"):
                try:
                    return json.loads(h.split(": ", 1)[1])
                except ValueError:
                    return {}
    return {}


def run_started(steps: list[dict]) -> datetime:
    """この回の開始時刻（最初のステップの終了時刻－秒数。ランナーは TZ=Asia/Tokyo）。記録が無ければ6時間前。"""
    jst = ZoneInfo("Asia/Tokyo")
    starts = []
    for s in steps:
        try:
            starts.append(datetime.strptime(s["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=jst) - timedelta(seconds=s["seconds"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(starts) if starts else datetime.now(timezone.utc) - timedelta(hours=6)


def _q(client, sql: str, params: list, location: str = "asia-northeast1") -> list:
    """1本のクエリ。表がまだ無い・失敗した時は [] を返し、他の項目は出す。"""
    from google.cloud import bigquery
    jc = bigquery.QueryJobConfig(query_parameters=params)
    try:
        return list(client.query(sql, job_config=jc, location=location).result())
    except Exception as e:
        print(f"  ⚠️ 通知: 1項目を引けない（{type(e).__name__}）")
        return []


def digest_jp(client, steps: list[dict]) -> list[tuple[str, list[str]]]:
    """政府 EDINET（書類一覧・大量保有）・有報の本文・EdinetDB の追加の窓口で、この回に入れたもの。"""
    from google.cloud import bigquery
    p, out = client.project, []
    since = run_started(steps).astimezone(timezone.utc)
    sp = [bigquery.ScalarQueryParameter("s", "TIMESTAMP", since)]
    rng = step_json(steps, "edinet_gov_docs").get("range")
    if rng:
        # この回に取った書類だけを数える（提出日の範囲で数えると、夜の遡りでは毎回ほぼ同じ全期間の集計になる）
        rows = _q(client, f"SELECT doc_type_code t, COUNT(*) n, MIN(submit_date) a, MAX(submit_date) b "
                          f"FROM `{p}.edinet.gov_documents` WHERE fetched_at >= @s GROUP BY t ORDER BY n DESC", sp)
        if rows:
            kinds = "・".join(f"{DOC_TYPES.get(r.t, r.t or '種別なし')} {r.n:,}" for r in rows[:8])
            a, b = min(r.a for r in rows), max(r.b for r in rows)
            out.append((f"政府 EDINET の書類一覧（この回に取った分・提出日 {a}〜{b}）",
                        [f"{sum(r.n for r in rows):,}件：{kinds}"]))
        # 表関数 large_holdings_between は提出日の範囲の分だけ読む（view は毎回表全体を読む）
        dr = [bigquery.ScalarQueryParameter("a", "DATE", a), bigquery.ScalarQueryParameter("b", "DATE", b)] if rows else []
        rows = [] if not rows else _q(client, f"""SELECT issuer_name, issuer_sec_code, holder_name, prior_ratio, holding_ratio, LEFT(purpose, 24) purpose
                  FROM `{p}.edinet.large_holdings_between`(@a, @b)
                  WHERE doc_id IN (SELECT doc_id FROM `{p}.edinet.gov_documents` WHERE fetched_at >= @s)
                    AND holding_ratio IS NOT NULL
                  QUALIFY ROW_NUMBER() OVER (PARTITION BY issuer_name, holder_name ORDER BY submit_date DESC) = 1
                  ORDER BY ABS(holding_ratio - IFNULL(prior_ratio, 0)) DESC LIMIT 8""", sp + dr)
        if rows:
            out.append(("大量保有で保有割合が大きく動いたもの（この回に取った書類）", [
                f"{r.issuer_name}（{r.issuer_sec_code or '-'}）: {r.holder_name} "
                f"{'新規' if r.prior_ratio is None else f'{r.prior_ratio * 100:.2f}%'} → {r.holding_ratio * 100:.2f}%"
                f"（{r.purpose or ''}）" for r in rows]))
    rows = _q(client, f"""SELECT COUNT(DISTINCT g.edinet_code) n, ARRAY_AGG(DISTINCT c.name IGNORE NULLS LIMIT 10) names
              FROM `{p}.edinet.gov_text_blocks` g LEFT JOIN `{p}.edinet.company` c USING (edinet_code)
              WHERE g.fetched_at >= @s""", sp)
    if rows and rows[0].n:
        out.append((f"有報の本文を取った会社 {rows[0].n:,}社", ["、".join(rows[0].names or []) + (" ほか" if rows[0].n > 10 else "")]))
    rows = _q(client, f"""SELECT endpoint, COUNT(*) n, ARRAY_AGG(c.name IGNORE NULLS LIMIT 6) names
              FROM `{p}.edinet.edinetdb_extras` e LEFT JOIN `{p}.edinet.company` c USING (edinet_code)
              WHERE e.fetched_at >= @s GROUP BY endpoint ORDER BY n DESC""", sp)
    if rows:
        out.append(("EdinetDB の追加の窓口", [f"{r.endpoint} {r.n}件：{'、'.join(r.names or [])}" for r in rows]))
    return out


def digest_sec(client, steps: list[dict]) -> list[tuple[str, list[str]]]:
    """SEC の提出一覧・8-K の項目・13D/13G で、この回に入れた日の範囲のもの。"""
    from google.cloud import bigquery
    rng = step_json(steps, "sec_filings").get("range")
    if not rng:
        return []
    p, out = client.project, []
    rp = [bigquery.ScalarQueryParameter("a", "DATE", rng[0]), bigquery.ScalarQueryParameter("b", "DATE", rng[1])]
    rows = _q(client, f"SELECT COUNT(*) n, COUNTIF(form IN ('10-K','10-Q')) fin, COUNTIF(form LIKE '8-K%') k8 "
                      f"FROM `{p}.us.sec_filings` WHERE filed_date BETWEEN @a AND @b", rp, "US")
    if rows:
        r = rows[0]
        out.append((f"SEC の提出一覧（{rng[0]}〜{rng[1]}）", [f"{r.n:,}件（10-K/10-Q {r.fin:,}・8-K {r.k8:,}）"]))
    rows = _q(client, f"""SELECT item, COUNT(*) n FROM `{p}.us.sec_filings`, UNNEST(SPLIT(items)) item
              WHERE filed_date BETWEEN @a AND @b AND items IS NOT NULL GROUP BY item ORDER BY n DESC LIMIT 8""", rp, "US")
    if rows:
        out.append(("8-K の項目", ["・".join(f"{ITEMS_8K.get(r.item, r.item)}（{r.item}）{r.n}" for r in rows)]))
    rows = _q(client, f"""SELECT issuer_name, person_name, person_type, percent FROM `{p}.us.sec_13dg`
              WHERE filed_date BETWEEN @a AND @b AND form LIKE 'SCHEDULE 13D%' AND percent IS NOT NULL
                AND NOT REGEXP_CONTAINS(IFNULL(issuer_name, ''), r'(?i)\b(fund|trust)\b')   -- ファンド同士の持ち合いは外す
              QUALIFY ROW_NUMBER() OVER (PARTITION BY issuer_name ORDER BY percent DESC) = 1
              ORDER BY percent DESC LIMIT 8""", rp, "US")
    if rows:
        out.append(("13D（経営に関わる目的もありうる5%超の保有）", [
            f"{r.issuer_name}: {r.person_name}（{r.person_type or '-'}）{r.percent:.1f}%" for r in rows]))
    rows = _q(client, f"SELECT COUNT(DISTINCT accession) n FROM `{p}.us.sec_13dg` "
                      "WHERE filed_date BETWEEN @a AND @b AND form LIKE 'SCHEDULE 13G%'", rp, "US")
    if rows and rows[0].n:
        out.append(("13G（純投資の5%超の保有）", [f"{rows[0].n:,}件"]))
    return out


def digest_price(client, steps: list[dict]) -> list[tuple[str, list[str]]]:
    from google.cloud import bigquery
    since = run_started(steps).astimezone(timezone.utc)
    rows = _q(client, f"SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, MIN(trade_date) a, MAX(trade_date) b "
                      f"FROM `{client.project}.analytics.price_daily` WHERE ingested_at >= @s",
              [bigquery.ScalarQueryParameter("s", "TIMESTAMP", since)])
    if not rows or not rows[0].n:
        return []
    r = rows[0]
    return [("取り込んだ終値", [f"{r.t:,}銘柄・{r.n:,}行（{r.a}〜{r.b}）"])]


def digest_positioning(client, steps: list[dict]) -> list[tuple[str, list[str]]]:
    """FINRA の空売り残・CFTC の建玉明細で、この回に入れた範囲のもの。"""
    from google.cloud import bigquery
    st, p, out = step_json(steps, "market_positioning"), client.project, []
    rng = st.get("short_range")
    if rng:
        rp = [bigquery.ScalarQueryParameter("d", "DATE", rng[1])]
        rows = _q(client, f"""SELECT symbol, issue_name, change_pct, days_to_cover, short_qty FROM `{p}.analytics.short_interest`
                  WHERE settlement_date = @d AND short_qty >= 1e6 AND change_pct IS NOT NULL
                  ORDER BY change_pct DESC LIMIT 8""", rp)
        out.append((f"空売り残（基準日 {rng[0]}〜{rng[1]}・{st.get('short_rows', 0):,}行）", [
            f"{r.symbol} {r.issue_name or ''}: 前回比 {r.change_pct:+.0f}%・{r.days_to_cover or 0:.1f}日分" for r in rows]
            or ["（100万株以上で増えた銘柄なし）"]))
    rng = st.get("cot_range")
    if rng:
        out.append((f"建玉明細 COT（{rng[0]}〜{rng[1]}）", [f"{st.get('cot_rows', 0):,}行（{st.get('cot_weeks', 0)}週×報告）"]))
    return out


US_FACTS_SQL = """SELECT ticker, name, latest_10k_filed k, latest_10q_filed q,
  COUNT(*) OVER () n,
  COUNTIF(latest_10k_filed >= DATE_SUB(DATE(@s), INTERVAL 7 DAY)) OVER () nk,
  COUNTIF(latest_10q_filed >= DATE_SUB(DATE(@s), INTERVAL 7 DAY)) OVER () nq
FROM `{p}.us.company_profile` WHERE facts_loaded_at >= @s
ORDER BY GREATEST(IFNULL(latest_10k_filed, DATE '1900-01-01'), IFNULL(latest_10q_filed, DATE '1900-01-01')) DESC, ticker
LIMIT 12"""


def digest_us_facts(client, steps: list[dict]) -> list[tuple[str, list[str]]]:
    """us.company_facts を取り直した会社（この回に facts_loaded_at が付いた会社）と、そのうち直近7日の 10-K/10-Q。"""
    from google.cloud import bigquery
    since = run_started(steps).astimezone(timezone.utc)
    rows = _q(client, US_FACTS_SQL.format(p=client.project), [bigquery.ScalarQueryParameter("s", "TIMESTAMP", since)], "US")
    if not rows:
        return []
    r0 = rows[0]
    cut = (since - timedelta(days=7)).date()

    def _form(r):
        kinds = [f"10-K {r.k}" if r.k and r.k >= cut else "", f"10-Q {r.q}" if r.q and r.q >= cut else ""]
        return "・".join(k for k in kinds if k) or "提出日は7日より前"

    return [(f"財務を取り直した会社 {r0.n:,}社（直近7日の 10-K {r0.nk}社・10-Q {r0.nq}社）",
             [f"{r.ticker} {r.name or ''}: {_form(r)}" for r in rows] + ([f"ほか {r0.n - len(rows)}社"] if r0.n > len(rows) else []))]


DIGESTS = {"fetch_jp_filings": digest_jp, "edinet_gov_backfill": digest_jp, "sec_filings": digest_sec,
           "price_universe": digest_price, "market_positioning": digest_positioning, "us_company_facts": digest_us_facts}


def build_mail(title: str, workflow: str, steps: list[dict], counts: dict, delta: dict,
               storage: dict | None, run_url: str, now: datetime, digest: list | None = None) -> tuple[str, str]:
    """(件名, HTML 本文)（純関数）。"""
    failed = [s for s in steps if s.get("rc")]
    mark = "❌" if failed else ("⚠️" if any(s.get("err") for s in steps) else "✅")
    # 件名の頭の【取得】で Gmail のフィルタが振り分ける（日次メールは【投資AIエージェント】）
    subject = f"【取得】{mark} {title} {now:%m/%d %H:%M}"
    parts = [f"<h3>{html.escape(title)}（{html.escape(workflow)}）{mark}</h3>"]
    if not steps:
        parts.append("<div>ステップの記録が無い（取り込みの前に止まった可能性。実行ページを見る）</div>")
    for s in steps:
        head = (f"{'✅' if not s.get('rc') else '❌'} <b>{html.escape(s['step'])}</b>"
                f"（{s.get('seconds', 0)}秒・✅{s.get('ok', 0)} ⚠️{s.get('warn', 0)} ❌{s.get('err', 0)}）")
        items = "".join(f"<li>{html.escape(h)}</li>" for h in s.get("highlights") or [])
        parts.append(f"<div style='margin-top:8px'>{head}<ul style='margin:2px 0'>{items}</ul></div>")
    if digest:
        parts.append("<h4>今回取ったもの</h4>")
        for head, lines in digest:
            items = "".join(f"<li>{html.escape(x)}</li>" for x in lines)
            parts.append(f"<div style='margin-top:4px'><b>{html.escape(head)}</b><ul style='margin:2px 0'>{items}</ul></div>")
    if counts:
        rows = ""
        for t, v in counts.items():
            d = delta.get(t)
            dtxt = "—" if d is None else f"{d:+,}"
            body = (f"<td>{v['rows']:,}</td><td>{dtxt}</td><td>{v['mb']:,}</td>" if "rows" in v
                    else f"<td colspan=3>読めない（{html.escape(v.get('error', ''))}）</td>")
            rows += f"<tr><td>{html.escape(t)}</td>{body}</tr>"
        parts.append("<h4>取り込み先の表</h4><table border=1 cellpadding=4 style='border-collapse:collapse;font-size:12px'>"
                     "<tr><th>表</th><th>行数</th><th>前回からの増減</th><th>MB</th></tr>" + rows + "</table>")
    if storage and storage.get("total_gb") is not None:
        parts.append(f"<div style='margin-top:8px'>BigQuery の保存量 {storage['total_gb']}GB（無料枠 10GB/月・"
                     f"測った時刻 {html.escape(str(storage.get('measured_at', '')))}）</div>")
    if run_url:
        parts.append(f"<div style='margin-top:8px'><a href='{html.escape(run_url)}'>実行ページ</a></div>")
    return subject, "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--tables", default="", help="行数の増減を見る表（dataset.table をカンマ区切り）")
    ap.add_argument("--base", default=os.environ.get("STOCK_ANALYZER_BASE") or "stock_data")
    a = ap.parse_args(argv)
    import config as cfg
    if not getattr(cfg, "WORKFLOW_NOTIFY_ENABLED", True):
        print("⏩ 通知: WORKFLOW_NOTIFY_ENABLED=False")
        return 0
    base = Path(a.base)
    steps = read_steps(actions_log.results_path(base, a.workflow))
    tables = [t.strip() for t in a.tables.split(",") if t.strip()]
    counts, delta = {}, {}
    state_p = base / "data" / "workflow_notify_counts.json"
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    client, digest = None, []
    if tables or a.workflow in DIGESTS:
        try:
            import state_store
            client = state_store.make_client(cfg)
        except Exception as e:
            print(f"⚠️ 通知: BigQuery に繋げない（{type(e).__name__}）")
    if client is not None and a.workflow in DIGESTS:
        try:
            digest = DIGESTS[a.workflow](client, steps)
        except Exception as e:
            print(f"⚠️ 通知: 今回取ったものを引けない（{type(e).__name__}）")
    if tables and client is not None:
        try:
            counts = table_counts(client, tables)
            delta = deltas(counts, state.get(a.workflow))
            state[a.workflow] = counts
            state_p.parent.mkdir(parents=True, exist_ok=True)
            state_p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            print(f"⚠️ 通知: 表の行数を読めない（{type(e).__name__}）")
    try:
        storage = json.loads((base / "logs" / "bq_storage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        storage = None
    srv, repo, rid = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    run_url = f"{srv}/{repo}/actions/runs/{rid}" if srv and repo and rid else ""
    subject, body = build_mail(a.title, a.workflow, steps, counts, delta, storage, run_url,
                               datetime.now(ZoneInfo("Asia/Tokyo")), digest)
    from alert_notifier import send_gmail
    ok, _err = send_gmail(os.environ.get("ALERT_GMAIL_FROM", ""), os.environ.get("ALERT_GMAIL_PASSWORD", ""),
                          os.environ.get("ALERT_GMAIL_TO", ""), subject, body)
    # 送れなかった理由はアドレスを含むことがあるので出さない（鍵の死活確認の Gmail 行で原因を見る）
    print("✅ 通知メールを送った" if ok else "⚠️ 通知メールを送れなかった")
    return 0


if __name__ == "__main__":
    sys.exit(main())
