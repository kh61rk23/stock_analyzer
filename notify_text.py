# ==============================================================
# notify_text.py — 取得メール（workflow_notify.py）の文面を日本語の文にする
# ==============================================================
# 取得の各段（actions_log.py --step <名前>）と、jp_filings の中のモジュールが出す ✅⚠️❌⏩ の1行を、
#   ・その段が何をするのか（STEPS / MODULES の表。説明はここ1か所だけ）
#   ・結果の文（件数を日本語で）
#   ・警告・見送り・失敗のときは「何が起きて・データにどう影響し・次に何が起きるか」
# に直す。段が出す行（JSON や辞書のダンプ）は変えず、メールにするときにここで読み替える
# （行は新旧どちらの形でも読めるよう、項目は有るものだけ拾う）。
# 表に無い段・行は、印と本文をそのまま出す（読み替えられなくてもメールは落ちない）。
# ==============================================================
from __future__ import annotations

import ast
import json
import re

# ── 何をする段か（段の名前 → (見出し, 説明)）──────────────────
STEPS: dict[str, tuple[str, str]] = {
    "jp_filings": ("日本企業の開示データのまとめ取得",
                   "EdinetDB・不動産DB・政策DB など日本企業の開示や公的データを取り、BQ に入れる。中のモジュールごとの結果を下に並べる。"),
    "edinet_gov_docs": ("政府 EDINET の書類一覧と中身",
                        "直近に提出された書類の一覧を取り、大量保有報告書・臨時報告書などの中身（数値）を読んで BQ に入れる。"
                        "残りの時間で過去分（最大10年前まで）へ遡る。"),
    "edinet_gov_text": ("有価証券報告書の本文",
                        "有報の「事業等のリスク」などの本文と、セグメント別の売上を取って BQ に入れる。"),
    "risk_text_scorer": ("有報のリスク記述の変化の採点",
                         "有報の「事業等のリスク」を前期と比べ、重い語（債務超過・減損など）が増えた会社に印を付けて BQ に書く。"),
    "sec_filings": ("SEC EDGAR の提出一覧",
                    "米国 SEC に提出された書類の一覧、8-K の項目（何の報告か）、13D/13G（5%超の大量保有）を日ごとに取って BQ に入れる。"),
    "price_universe": ("全上場株の終値",
                       "日本株・米国株の全上場銘柄の終値を取って BQ に入れる。取れない銘柄は上場廃止の疑いとして数える。"),
    "us_company_facts": ("米国企業の財務データ",
                         "10-K/10-Q を新しく出した米国企業の財務データを SEC から取り直し、BQ に入れる。"),
    "us_segments": ("米国企業のセグメント別財務",
                    "SEC が四半期ごとに公開する財務データの zip から、セグメント別の数値を BQ に入れる（新しい四半期が出たときだけ動く）。"),
    "us_exposure_builder": ("米国企業の地域別の売上の偏り",
                            "セグメント別の売上から、どの国・地域にどれだけ売上を依存しているかを作り直して BQ に書く。"),
    "us_taxonomy_mapper": ("米国企業の事業分類の割り当て",
                           "米国企業を事業の分類（セグメント名・SIC 業種コード）に結びつけ直して BQ に書く。"),
    "market_positioning": ("需給のデータ（空売り残・建玉明細）",
                           "FINRA の空売り残（月2回）と CFTC の建玉明細 COT（週1回）を取って BQ に入れ、売買シグナルを作り直す。"),
    "backfill": ("EdinetDB の会社ページからの財務補完",
                 "EdinetDB の会社ページから、取れていない年度の財務を補って BQ に入れる。"),
    # 手動タスク（manual_task の段の名前は選んだ処理の名前）
    "bq_compact_price_dedupe": ("手動：株価表の重複を畳む", "株価の表に重なっている行を1行にまとめ、保存量を減らす。"),
    "bq_compact_price_monthly_dry": ("手動：古い株価の月次化（試算）", "古い日次の株価を月次に畳むとどれだけ減るかを、BQ を書き換えずに試算する。"),
    "bq_compact_price_monthly": ("手動：古い株価の月次化", "境目より古い日次の株価を月次に畳み、保存量を減らす。"),
    "bq_compact_sec_forms": ("手動：SEC の提出書類表の圧縮", "SEC の提出一覧のうち使わない書類種別を畳み、保存量を減らす。"),
    "bq_compact_gov_facts": ("手動：政府 EDINET の数値表の圧縮", "政府 EDINET の数値（XBRL）表の重複・不要な行を畳み、保存量を減らす。"),
    "bq_compact_cot_legacy": ("手動：COT（旧形式）の圧縮", "CFTC の建玉明細（旧形式）から使う市場だけを残し、保存量を減らす。"),
    "price_repair_splits": ("手動：株式分割で跳んだ株価の取り直し", "株価が分割で不連続になった銘柄を見つけ、その銘柄の履歴を取り直す。"),
    "us_company_facts_update": ("手動：米国企業の財務の更新", "米国企業の財務データを、新しく提出された会社分だけ取り直す。"),
    "us_company_facts_backfill": ("手動：米国企業の財務の全社取り直し", "米国企業の財務データを、時間の許す限り全社分取り直す。"),
    "us_company_facts_segments": ("手動：米国企業のセグメント別財務", "SEC の四半期 zip からセグメント別の数値を取り込む。"),
    "taxonomy_mapper": ("手動：企業の事業分類の割り当て", "企業を事業の分類に結びつけ直して BQ に書く。"),
    "edinet_gov_probe": ("手動：政府 EDINET の不動産項目の確認", "指定した会社の有報から、投資不動産の項目が読めるかを試す（BQ には書かない）。"),
    "edinet_gov_segment_probe": ("手動：有報のセグメント売上の確認", "指定した会社の有報から、セグメント別売上が読めるかを試す（BQ には書かない）。"),
    "edinet_gov_fv_ingest": ("手動：政府 EDINET の賃貸等不動産の公正価値", "有報から賃貸等不動産の帳簿価額と公正価値を読んで BQ に入れる。"),
    "seisakudb_probe": ("手動：政策DB の応答の形の確認", "政策DB の各窓口を1件ずつ呼び、応答の形を確かめる（約8回の呼び出し）。"),
}

# jp_filings の中のモジュール（台帳の module 名 → (見出し, 説明)）
MODULES: dict[str, tuple[str, str]] = {
    "rental_property_fv": ("賃貸等不動産の公正価値（EdinetDB）", "有報の注記にある賃貸等不動産の帳簿価額・公正価値を EdinetDB から取る。"),
    "edinet_gov_fv": ("賃貸等不動産の公正価値（政府 EDINET）", "政府 EDINET の有報から賃貸等不動産の帳簿価額・公正価値を読む。"),
    "edinetdb_ingest": ("EdinetDB の財務・有報", "EdinetDB から上場企業の財務と有報の情報を取る。"),
    "edinet_doc_index": ("有報の書類索引", "有報のどこに何が書いてあるかの索引を作る。"),
    "reit_ingest": ("REIT の物件", "REIT の保有物件と鑑定評価を取る（週1回）。"),
    "fudosan_ingest": ("不動産DB（企業の保有不動産）", "企業が持つ不動産とエリア情報を不動産DB から取る。"),
    "edinetdb_extras": ("EdinetDB の追加の窓口", "決算・持ち合い株・子会社・主要顧客を会社ごとに少しずつ取り、全社を一巡する。"),
    "seisakudb": ("政策DB（公的支出・経済安保）",
                  "上場企業が国から受けた補助金・調達と、経済安保の認定を政策DB から取る。1日950回まで、1秒おきに呼ぶ。"),
}

# 項目名の日本語（表に無い辞書のダンプを読み替えるときに使う）
KEY_JA = {"days": "日数", "docs": "書類", "filings": "提出書類", "items": "項目", "errors": "失敗", "rows": "行", "range": "範囲",
          "oldest": "遡り済み", "storage_gb": "BQ の保存量GB", "inserted": "追加", "targets": "対象", "skipped": "見送り",
          "success": "成功", "failure": "失敗", "companies": "会社", "countries": "国", "profiles": "企業", "written": "書き込み"}

_MARK = {"✅": "✅", "❌": "❌", "⏩": "⏩", "⚠": "⚠️", "📥": "✅", "↩": "⏩"}
_HL = re.compile(r"^\s*(✅|❌|⏩|⚠️?|📥|↩️?)\s*(?:\[(\w+)\]\s*)?(.*)$", re.S)
_NAME = re.compile(r"^(\w+)\s*:\s*(.*)$", re.S)
_SCALAR = re.compile(r"""["']?(\w+)["']?\s*:\s*(-?\d+(?:\.\d+)?|true|false|True|False|None|null|"[^"]*"|'[^']*')""")
_RANGE = re.compile(r"""["']?(\w*range)["']?\s*:\s*\[\s*["']([\d-]+)["']\s*,\s*["']([\d-]+)["']""")
_DAY_ERR = re.compile(r"^一覧 (\d{4}-\d{2}-\d{2}): (.*)$")


def parse_highlight(h: str) -> tuple[str, str, str, str]:
    """要点の1行 → (印, 台帳の状態 [success] など, 名前, 本文)。名前の無い行は名前が空。"""
    m = _HL.match(h or "")
    if not m:
        return "⚠️", "", "", (h or "").strip()
    mark, tag, body = _MARK.get(m.group(1)[0], "⚠️"), m.group(2) or "", m.group(3).strip()
    n = _NAME.match(body)
    return (mark, tag, n.group(1), n.group(2)) if n else (mark, tag, "", body)


def loose_dict(text: str) -> dict:
    """辞書のダンプ（JSON・Python の repr・途中で切れたもの）から、数値・真偽・短い文字・日付の範囲を拾う。"""
    t = (text or "").strip()
    if t.startswith("{"):
        for loader in (json.loads, ast.literal_eval):
            try:
                v = loader(t)
                if isinstance(v, dict):
                    return v
            except (ValueError, SyntaxError):
                pass
    out: dict = {}
    for k, v in _SCALAR.findall(t):
        v = v.strip("\"'")
        if v in ("true", "True"):
            out[k] = True
        elif v in ("false", "False"):
            out[k] = False
        elif v in ("None", "null"):
            out[k] = None
        else:
            try:
                out[k] = float(v) if "." in v else int(v)
            except ValueError:
                out[k] = v
    for k, a, b in _RANGE.findall(t):
        out[k] = [a, b]
    return out


def _n(d: dict, k: str) -> int:
    v = d.get(k)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _span(rng) -> str:
    return f"{rng[0]}〜{rng[1]}" if rng and len(rng) == 2 else ""


def reason_ja(reasons: str) -> str:
    """失敗の理由（「API 404 Not Found×10」など）を日本語にする。"""
    s = str(reasons or "")
    parts = []
    for key, ja in (("404", "相手にその日付・書類が無い（404）"), ("400", "相手が書類を受け付けない（400）"),
                    ("401", "キーが認識されない（401）"), ("403", "キーの権限がない・遮断された（403）"),
                    ("429", "回数制限（429）"), ("HTML", "メンテナンス・遮断の疑い（HTML が返った）")):
        if key in s:
            parts.append(ja)
    return "・".join(parts) or s[:80]


Line = tuple[str, str]   # (印, 文)


# ── 段ごとの読み替え ─────────────────────────────────────────

def fmt_edinet_gov_docs(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    if d.get("skipped"):
        return [("⏩", "政府 EDINET のキーが未設定のため何もしなかった。")]
    out: list[Line] = []
    days, rng = _n(d, "days"), _span(d.get("range"))
    head = (f"書類一覧を{days:,}日分確認（提出書類 {_n(d, 'docs'):,}件）。大量保有・臨時報告書など {_n(d, 'fact_docs'):,}件の中身を読み、"
            f"数値 {_n(d, 'facts'):,}行を BQ に入れた" + (f"（提出日 {rng}）。" if rng else "。"))
    errors = _n(d, "errors")
    stopped = d.get("api_stopped")
    fatal = bool(d.get("api_fatal"))
    out.append(("✅" if not errors and not d.get("facts_paused") else "⚠️", head))
    if d.get("backfill_end"):
        out.append(("✅", f"過去分の取り込みは完了。EDINET は提出から10年分しか持たず、{d['backfill_end']} より前は 404 が返る"
                          "（取れなかったのではなく範囲の端）。次：以降は直近7日の取り直しだけをする。"))
    elif d.get("oldest") and d.get("budget_out"):
        out.append(("⏩", f"時間の予算で区切った。遡りは {d['oldest']} まで済み。次：続きは次の回。"))
    if d.get("facts_paused"):
        out.append(("⚠️", "BQ の保存量が上限に近いため、中身（数値）の取り込みは止め、一覧だけ続けた。"
                          "影響：大量保有などの数値が増えない。次：保存量が下がれば自動で再開する。"))
    if errors:
        why = reason_ja(d.get("error_reasons", ""))
        if fatal:
            out.append(("❌", f"取れなかった日・書類が{errors}件（{why}）。キーの誤りか回数制限なので打ち切った。"
                              "影響：この回は新しい書類が BQ に入っていない。次：キー（EDINET_GOV_API_KEY）と回数を確認する。"))
        else:
            cut = "同じ失敗が続いたので、この回は途中で打ち切った。" if stopped else ""
            out.append(("⚠️", f"取れなかった日・書類が{errors}件（{why}）。{cut}"
                              "影響：その日の書類一覧（または中身）がこの回は BQ に入っていない。取れた日の分は入っている。"
                              "次：途中の日は記録していないので、次の回に同じ日からやり直す。放っておいてよい。"))
    return out


def fmt_edinet_gov_text(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    if d.get("skipped"):
        return [("⏩", f"有報の本文は取らなかった（{d.get('reason', '保存量の上限など')}）。次：保存量が下がれば再開する。")]
    out: list[Line] = [("✅" if not _n(d, "errors") else "⚠️",
                        f"有報など {_n(d, 'targets'):,}件を確認し、{_n(d, 'docs'):,}件から本文 {_n(d, 'rows'):,}行・"
                        f"セグメント売上 {_n(d, 'seg_rows'):,}行を BQ に入れた（新規 {_n(d, 'new_docs'):,}件・前期 {_n(d, 'prev_docs'):,}件）。")]
    notes = []
    if _n(d, "no_text"):
        notes.append(f"本文なし {_n(d, 'no_text'):,}件（書類に該当の節が無い。正常）")
    if _n(d, "gone"):
        notes.append(f"書類側で取れない {_n(d, 'gone'):,}件（取り下げなど。取り直しても同じなので済みにした）")
    if _n(d, "requeued"):
        notes.append(f"取り直しに戻した {_n(d, 'requeued'):,}件")
    if _n(d, "remaining"):
        notes.append(f"未処理の残り {_n(d, 'remaining'):,}件（次の回に続ける）")
    if notes:
        out.append(("⏩", "。".join(notes) + "。"))
    if _n(d, "errors"):
        out.append(("⚠️", f"取得に失敗した書類が{_n(d, 'errors'):,}件。影響：その書類の本文が BQ に入っていない。次：次の回に取り直す。"))
    return out


def fmt_risk_text_scorer(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    return [("✅", f"直近2期の有報がそろう {_n(d, 'companies_2y'):,}社を前期と比べ、悪化の印を付けたのは {_n(d, 'flagged'):,}社"
                   f"（{_n(d, 'written'):,}社分を BQ に書き直した）。")]


def fmt_sec_filings(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    if d.get("skipped"):
        return [("⏩", "SEC への連絡先（SEC_USER_AGENT）が未設定のため何もしなかった。")]
    rng, errors = _span(d.get("range")), _n(d, "errors")
    out: list[Line] = [("✅" if not errors else "⚠️",
                        f"提出一覧を{_n(d, 'days'):,}日分確認" + (f"（{rng}）" if rng else "") +
                        f"。提出書類 {_n(d, 'filings'):,}件・8-K の項目 {_n(d, 'items'):,}件・13D/13G {_n(d, 'dg_rows'):,}行を BQ に入れた。"
                        "土日・祝日は一覧が無いので「提出0件の日」として済みにしている。")]
    if d.get("budget_out") and d.get("oldest"):
        out.append(("⏩", f"時間の予算で区切った。遡りは {d['oldest']} まで済み。次：続きは次の回。"))
    if errors:
        out.append(("⚠️", f"取れなかった日・書類が{errors}件（SEC の遮断や回数制限など）。影響：その日の提出情報が BQ に入っていない。"
                          "次：記録していないので、次の回に同じ日からやり直す。放っておいてよい。"))
    return out


def fmt_market_positioning(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    sr, cr = _span(d.get("short_range")), _span(d.get("cot_range"))
    out: list[Line] = []
    out.append(("✅", (f"FINRA の空売り残：基準日 {sr} の {_n(d, 'short_rows'):,}行を入れた。" if sr else
                      "FINRA の空売り残：新しい基準日はなかった（公表は月2回）。")))
    out.append(("✅", (f"CFTC の建玉明細：{cr} の{_n(d, 'cot_weeks')}週分・{_n(d, 'cot_rows'):,}行を入れた。" if cr else
                      "CFTC の建玉明細：新しい週はなかった（公表は週1回）。")))
    if _n(d, "cot_backfill_weeks"):
        out.append(("⏩", f"建玉明細の取り直し {_n(d, 'cot_backfill_weeks')}週分も済ませた。"))
    if _n(d, "errors"):
        out.append(("⚠️", f"取れなかったものが{_n(d, 'errors')}件。影響：その月・週のデータが BQ に入っていない。次：次の回に取り直す。"))
    if d.get("budget_out"):
        out.append(("⏩", "時間の予算で区切った。次：続きは次の回。"))
    return out


_PU = re.compile(r"^(\w+)\s+取得(\d+)/(\d+)（([\d.]+%)・下限(\d+%)）\s*未取得(\d+)\s*dead(\d+)\s*行(\d+)\s*([\d.]+)秒")


def fmt_price_universe(mark: str, tag: str, rest: str, name: str = "") -> list[Line]:
    # 例: success jp 取得3817/3820（99.9%・下限90%） 未取得3 dead198 行3307 122.1秒
    m = _PU.match(re.sub(r"^(success|warn|error|alert|skipped)\s+", "\\1 ", rest.strip()).split(" ", 1)[-1].strip()) \
        if rest.strip() else None
    market = "日本株" if name.endswith("_jp") else "米国株" if name.endswith("_us") else "株"
    if not m:
        return [(mark, f"{market}の終値：{rest[:160]}")]
    _, hit, tot, rate, floor, missed, dead, rows, sec = m.groups()
    ok = mark == "✅"
    out: list[Line] = [(mark, f"{market}の終値：{int(tot):,}銘柄のうち {int(hit):,}銘柄を取得（{rate}・合格ラインは{floor}）。"
                              f"BQ に {int(rows):,}行入れた（{float(sec):.0f}秒）。")]
    if int(missed) or int(dead):
        out.append(("⏩" if ok else "⚠️", f"取れなかった銘柄 {missed}件・上場廃止とみなして外している銘柄（累計）{dead}件。"
                                        "影響：その銘柄の当日の終値だけが欠ける。次：次の回に取り直し、連続して取れない銘柄は廃止扱いにする。"))
    if not ok:
        out.append(("⚠️", f"取得率が合格ラインを下回った（{rate}<{floor}）。影響：当日の株価に欠けが多い。次：次の回に取り直す。"))
    return out


def fmt_us_company_facts(mark: str, tag: str, rest: str) -> list[Line]:
    return [(mark, rest.strip())]   # すでに日本語の文


def fmt_us_exposure_builder(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    return [("✅", f"{_n(d, 'companies'):,}社・{_n(d, 'countries')}か国の売上の地域別の偏りを {_n(d, 'rows'):,}行作り直し、BQ に書いた。")]


def fmt_us_taxonomy_mapper(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    per = d.get("items_per_company")
    return [("✅", f"米国企業 {_n(d, 'companies'):,}社を事業分類に結びつけ直し（{_n(d, 'rows'):,}行"
                   + (f"・1社あたり平均 {per}分類" if per else "") + f"・セグメント情報のある会社 {_n(d, 'segment_companies'):,}社）、BQ に書いた。")]


def fmt_edinetdb_ingest(mark: str, tag: str, rest: str) -> list[Line]:
    m = re.match(r"取込(\d+)社\s*/\s*データ無し(\d+)社\s*/\s*失敗(\d+)社\s*/\s*書類(\d+)件\s*/\s*API\s*(\d+)回", rest)
    if not m:
        return [(mark, rest[:200])]
    a, b, c, e, f = map(int, m.groups())
    out: list[Line] = [("✅" if not c else "⚠️", f"財務・有報を取り込んだ会社 {a}社（書類 {e}件）。EdinetDB にデータが無かった会社 {b}社・"
                                              f"失敗 {c}社。API を {f}回使った。")]
    if c:
        out.append(("⚠️", "失敗した会社は BQ に入っていない。次：次の回に取り直す。"))
    return out


def fmt_edinet_doc_index(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    return [("✅", f"索引の対象 {_n(d, 'targets')}書類のうち {_n(d, 'visited')}書類を開き、索引 {_n(d, 'inserted')}行を追加した"
                   f"（書類が無かった {_n(d, 'absent')}件・中身が空 {_n(d, 'empty')}件）。")]


def fmt_reit_ingest(mark: str, tag: str, rest: str) -> list[Line]:
    return [("⏩", f"REIT の物件の取り込みは週1回なので今回は見送り（{rest.replace('週1回', '').strip('（） ')}）。正常。次：1週間後に動く。")]


def fmt_fudosan_ingest(mark: str, tag: str, rest: str) -> list[Line]:
    d = loose_dict(rest)
    out: list[Line] = [("✅" if not _n(d, "failure") else "⚠️",
                        f"不動産DB：{_n(d, 'success')}社分を取り込んだ（失敗 {_n(d, 'failure')}社）。"
                        f"保有不動産 {_n(d, 'holdings'):,}件・エリア情報 {_n(d, 'area_profiles')}件を BQ に入れた。")]
    if _n(d, "failure"):
        out.append(("⚠️", "失敗した会社は次の回に取り直す。"))
    return out


_EXTRA_JA = {"earnings": "決算", "cross-shareholdings": "持ち合い株", "subsidiaries": "子会社", "main-customers": "主要顧客"}


def fmt_edinetdb_extras(mark: str, tag: str, rest: str) -> list[Line]:
    m = re.match(r"取得(\d+)件\s*/\s*データ無し(\d+)件", rest)
    prog = re.findall(r"'([\w-]+)':\s*'(\d+)/(\d+)'", rest)
    out: list[Line] = []
    if m:
        out.append((mark, f"追加の窓口から {m.group(1)}件を取得した（データが無かった {m.group(2)}件）。"))
    else:
        out.append((mark, rest[:200]))
    if prog:
        out.append(("⏩", "全社を一巡するまでの進み：" + "・".join(f"{_EXTRA_JA.get(k, k)} {int(a):,}/{int(b):,}社" for k, a, b in prog)
                    + "。次：毎回少しずつ進める。"))
    return out


def fmt_seisakudb(mark: str, tag: str, rest: str) -> list[Line]:
    m = re.match(r"直近(\d+)件\s*/\s*個社(\d+)社\s*/\s*経済安保(\d+)件\s*/\s*API\s*(\d+)回(?:\s*/\s*429で待った(\d+)回)?(?:\s*/\s*停止:\s*(.*))?", rest)
    if not m:
        return [(mark, rest[:200])]
    recent, comp, sec, calls, waited, stop = m.groups()
    out: list[Line] = [(mark, f"直近の公的支出 {int(recent):,}件・経済安保の認定 {int(sec):,}件・個社の受給履歴 {int(comp):,}社分を取得"
                              f"した（呼び出し {int(calls):,}回／1日950回まで）。")]
    stop = (stop or "").strip()
    if waited:
        out.append(("⏩", f"相手から「呼び出しが多い」（429）と返されて待ったのが{waited}回。待ってやり直せたので続けられた。"))
    if stop == "budget":
        out.append(("⏩", "1日の予算（950回）を使い切って終えた。正常。次：個社は最終取得日が古い順に、次の回で続きを取る。"))
    elif "429" in stop:
        out.append(("⚠️", "政策DB が「呼び出しが多すぎる」（429）と返し、待ってやり直しても続いたため、この回はここで止めた。"
                          "影響：個社の受給履歴の更新が予定より少ない（取れた分は BQ に入っている）。"
                          "次：残りの会社は最終取得日が古い順に、次の回で続きを取る。放っておいてよい（日次の分析は政策DB を呼ばず、この結果を写すだけ）。"))
    elif "401" in stop or "403" in stop:
        out.append(("❌", f"政策DB が認証を拒否した（{stop}）。影響：この回の政策DB のデータは増えていない。次：キー（SEISAKUDB_API_KEY）を確認する。"))
    elif stop:
        out.append(("⚠️", f"途中で止まった（{stop}）。次：次の回に続きを取る。"))
    return out


def fmt_jp_filings_summary(mark: str, tag: str, rest: str) -> list[Line]:
    m = re.match(r"(\d+)モジュール（成功(\d+)・見送り(\d+)・警告(\d+)・失敗(\d+)）(?:・要確認 ([^→]+))?", rest)
    if not m:
        return [("⏩", f"結果を記録した（{rest.split('→')[0].strip()}）。")] if rest.startswith("記録") else [(mark, rest[:200])]
    n, ok, skip, warn, err, bad = m.groups()
    bad_names = "、".join(MODULES.get(b.strip(), (b.strip(),))[0] for b in (bad or "").split("、") if b.strip())
    return [("✅" if not (int(warn) or int(err)) else "⚠️",
             f"{n}モジュールの結果：成功 {ok}・見送り {skip}（週1回の取り込みなど、今日は動かない正常な休み）・警告 {warn}・失敗 {err}"
             + (f"。確認が要るのは「{bad_names}」（下に理由）。" if bad_names else "。"))]


def fmt_generic(mark: str, tag: str, name: str, rest: str, step: str) -> list[Line]:
    d = loose_dict(rest) if rest.lstrip().startswith("{") else {}
    if d:
        pairs = [f"{KEY_JA.get(k, k)} {v}" for k, v in d.items() if not isinstance(v, (dict, list))][:10]
        body = "・".join(pairs) if pairs else "（詳細は実行ログ）"
        return [(mark, body)]
    m = re.match(r"^MERGE完了: (\d+)行", rest)
    if m:
        return [(mark, f"BQ への書き込みが完了（{int(m.group(1)):,}行）。")]
    return [(mark, rest if (not name or name == step) else f"{name}: {rest}")]


# 台帳の module 名・段の名前（本文の先頭の識別子）→ 読み替え
_FORMATTERS = {
    "edinet_gov_docs": fmt_edinet_gov_docs, "edinet_gov_text": fmt_edinet_gov_text, "risk_text_scorer": fmt_risk_text_scorer,
    "sec_filings": fmt_sec_filings, "market_positioning": fmt_market_positioning, "us_company_facts": fmt_us_company_facts,
    "us_exposure_builder": fmt_us_exposure_builder, "us_taxonomy_mapper": fmt_us_taxonomy_mapper,
    "edinetdb_ingest": fmt_edinetdb_ingest, "edinet_doc_index": fmt_edinet_doc_index, "reit_ingest": fmt_reit_ingest,
    "fudosan_ingest": fmt_fudosan_ingest, "edinetdb_extras": fmt_edinetdb_extras, "seisakudb": fmt_seisakudb,
}


def describe(step: str) -> tuple[str, str]:
    """段の名前 → (見出し, 何をする段か)。表に無ければ名前そのまま・説明なし。"""
    return STEPS.get(step) or MODULES.get(step) or (step, "")


def explain_step(rec: dict) -> dict:
    """ステップの記録（actions_log.step_record）→ {title, desc, mark, lines: [(印, 文)], modules: [(見出し, 説明)]}。"""
    step = rec.get("step", "")
    title, desc = describe(step)
    parsed = [parse_highlight(h) for h in rec.get("highlights") or []]
    has_summary = any(n == step and rest.lstrip().startswith("{") for _, _, n, rest in parsed)
    lines: list[Line] = []
    day_errs: list[tuple[str, str]] = []
    modules: list[tuple[str, str]] = []
    for mark, tag, name, rest in parsed:
        dm = _DAY_ERR.match(rest if not name else f"{name}: {rest}")
        if dm:
            day_errs.append((dm.group(1), dm.group(2)))
            continue
        if name in MODULES and name != step:
            modules.append(MODULES[name])
        if name == "jp_filings":
            lines += fmt_jp_filings_summary(mark, tag, rest)
        elif name.startswith("price_universe"):
            lines += fmt_price_universe(mark, tag, rest, name)
        elif name == "us_segments":
            lines.append((mark, rest.strip()))
        elif name in _FORMATTERS:
            fl = _FORMATTERS[name](mark, tag, rest)
            if name in MODULES:
                fl = [(m, f"【{MODULES[name][0]}】{t}" if i == 0 else t) for i, (m, t) in enumerate(fl)]
            lines += fl
        elif not name and "None " in rest and "（本文なしとして済みにする）" in rest:
            m = re.match(r"None (\S+): API (\d+)", rest)
            lines.append(("⏩", f"書類 {m.group(1) if m else ''} は相手側で取れない（{m.group(2) if m else ''}）ため本文なしとして済みにした。放っておいてよい。"))
        else:
            lines += fmt_generic(mark, tag, name, rest, step)
    if day_errs and not has_summary:
        days = sorted(d for d, _ in day_errs)
        why = reason_ja("・".join({r for _, r in day_errs}))
        lines.append(("⚠️", f"書類一覧を取れなかった日が{len(days)}日（{days[0]}〜{days[-1]}）：{why}。次：次の回にやり直す。"))
    mark = "❌" if rec.get("rc") else ("⚠️" if any(m in ("⚠️", "❌") for m, _ in lines) else "✅")
    return {"title": title, "desc": desc, "mark": mark, "lines": lines, "modules": modules}
