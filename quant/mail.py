# ==============================================================
# quant/mail.py — 検証ラボのメール・レポートの文
# ==============================================================
# 【役割】run_daily の戻り（daily）から日次メールの節の本文（HTML でない素の行）を作り、run_research が保存した
#   registry から全候補の成績表（markdown）を作る。純関数で、ファイルも設定も読まない。
# 【文体】paper/mail_section.py に合わせる（見出しは「■」・内訳は字下げ2つ・日付は M/D・数字は％で小数1桁）。
# 【見送りの日】skipped でも理由と注意書きは出す（build_alert の本文は理由の1行だけ）。
# 【名前を変えない】alert_notifier.py が build_alert(results.get("quant_lab")) を呼ぶ。
# ==============================================================
from __future__ import annotations

from quant import settings as S

CAUTIONS = (
    "※検証は過去の値動きに基づく見込みで、利益を約束するものではありません。発注はご自身の判断でお願いします。",
    "※オプションは理論価格での検証です（実際の売買の値段とは差が出ます）。",
)

_STATUS_JA = {"adopted": "採用", "watch": "観察", "rejected": "不採用"}


def _md(d) -> str:
    """'YYYY-MM-DD' を 'M/D' に。読めなければそのまま（空は '-'）。"""
    s = str(d or "")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{int(s[5:7])}/{int(s[8:10])}"
    return s or "-"


def _pct(v, signed: bool = False) -> str:
    """小数を％（小数1桁）に。値が無いときは '-'。"""
    if v is None:
        return "-"
    try:
        f = float(v) * 100.0
    except (TypeError, ValueError):
        return "-"
    if f != f or f in (float("inf"), float("-inf")):
        return "-"
    return f"{f:+.1f}%" if signed else f"{f:.1f}%"


def _num(v, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    if f != f or f in (float("inf"), float("-inf")):
        return "-"
    return f"{f:.{nd}f}"


def _proposal_lines(proposals: list) -> list:
    """提案を資産クラスごとにまとめて行にする。"""
    groups: dict = {}
    for p in proposals:
        groups.setdefault(p.get("asset_class"), []).append(p)
    out = []
    for _cls, items in groups.items():
        out.append(f"【{items[0].get('label') or _cls}】")
        for p in items:
            hint = f"（{p['exec_hint']}）" if p.get("exec_hint") else ""
            head = f"  ・{p.get('name') or p.get('symbol')}{hint}："
            if p.get("action") == "閉じる":
                out.append(f"{head}{p['action']}（資金の0%）")
            else:
                out.append(f"{head}{p.get('side')}・資金の{float(p.get('weight_pct') or 0):.1f}%・{p.get('action')}")
            if p.get("asset_class") == "options" and p.get("note"):
                out.append(f"    {p['note']}")
    return out


def lines(daily) -> list:
    """日次メールの節の本文（行のリスト）。daily が空なら空のリスト。skipped・採用なし・採用ありのどれでも注意書きを含む。"""
    if not daily:
        return []
    if daily.get("skipped"):
        return [f"検証ラボは今日は見送り：{daily.get('reason') or '理由は記録なし'}", *CAUTIONS]

    # 既存の仮想トレードの採用は下の1行で別に書くので、本数には数えない
    ext = set(daily.get("external_adopted") or [])
    adopted = [i for i in daily.get("adopted") or [] if i not in ext]
    watch = list(daily.get("watch") or [])
    out = [f"{_md(daily.get('registry_as_of'))}時点の検証：採用 {len(adopted)}本・観察中 {len(watch)}本"
           f"（今日の価格は {_md(daily.get('as_of'))} まで）"]

    if daily.get("status") == "no_adoption":
        out.append("■ 採用した戦略なし（現金のまま）")
        reasons = daily.get("top_reasons") or []
        if reasons:
            out.append("  採用に足りなかった点（多い順）：" + "・".join(f"{r['label']}（{r['count']}本）" for r in reasons))
    elif "active" in daily and not daily.get("active"):
        # 採用はあるが全部停止中：組み合わせの見込みは建てていない玉の話になるので出さない
        out.append("■ 採用した戦略はすべて停止中（実績の悪化。今日は現金のまま）")
    else:
        port = daily.get("portfolio") or {}
        met = port.get("metrics") or {}
        pp = (port.get("p_profit") or {}).get("252")
        out.append("■ 採用した組み合わせの見込み")
        out.append(f"  12か月後に利益が出ている確率：{_pct(pp)}")
        out.append(f"  年率の見込み：{_pct(met.get('ann_return'), signed=True)}（変動 {_pct(met.get('ann_vol'))}）"
                   f"・検証での最大の下げ：{_pct(met.get('max_drawdown'))}")
        brake = port.get("brake_now")
        if brake is not None and float(brake) < 1.0:
            out.append(f"  損失ブレーキ：建玉を{float(brake):g}倍に絞っています")
    if watch and daily.get("status") == "no_adoption":
        out.append(f"  観察中の戦略が{len(watch)}本あります（基準に届くまで提案は出しません）。基準に近い順：")
        for wt in daily.get("watch_top") or []:
            out.append(f"   ・{wt.get('label')}：確からしさ {_pct(wt.get('dsr'))}（基準 95.0%）"
                       f"・12か月後に利益の確率 {_pct(wt.get('p_profit_12m'))}・検証での最大の下げ {_pct(wt.get('max_drawdown'))}")

    for sid in daily.get("external_adopted") or []:
        out.append(f"  既存の仮想トレード（{sid}）も採用の基準を満たしました（提案は仮想トレード側で出ています）")

    proposals = list(daily.get("proposals") or [])
    n_open = sum(1 for p in proposals if p.get("action") != "閉じる")
    if proposals:
        out.append(f"■ 今日の提案（{len(proposals)}件・うち建てる・調整 {n_open}件）")
        out += _proposal_lines(proposals)
    elif daily.get("status") != "no_adoption" and daily.get("active", True):
        out.append("■ 今日の提案はありません（組み合わせの倍率が 0 など、建てる玉が無い日）")

    susp = list(daily.get("suspended") or [])
    if susp:
        out.append("■ 停止した戦略（実績が検証より悪く、しばらく提案から外します）")
        for s in susp:
            until = f"（{_md(s.get('until'))} まで）" if s.get("until") else ""
            out.append(f"  ・{s.get('label') or s.get('id')}：{s.get('reason') or ''}{until}")

    out.append("■ 注意")
    out += [f"  {c}" for c in CAUTIONS]
    return out


def build_alert(daily) -> dict | None:
    """日次メールの節（alert 形式）。daily が None・空なら None。skipped は理由1行だけを本文にする。"""
    if not daily:
        return None
    if daily.get("skipped"):
        return {"trigger": "quant_lab", "title": "🔬 検証ラボ（見送り）",
                "body": str(daily.get("reason") or "理由は記録なし"), "severity": "info"}
    n_a = len(daily.get("adopted") or [])
    n_p = len(daily.get("proposals") or [])
    return {"trigger": "quant_lab", "title": f"🔬 検証ラボ（採用 {n_a}本・提案 {n_p}件）",
            "body": "\n".join(lines(daily)), "severity": "info"}


def _cell(s) -> str:
    return str(s).replace("|", "／").replace("\n", " ")


def _sort_key(c: dict):
    order = {"adopted": 0, "watch": 1, "rejected": 2}
    sh = (c.get("metrics") or {}).get("sharpe")
    return (order.get(c.get("status"), 3), -(float(sh) if isinstance(sh, (int, float)) else -99.0))


def _failed_items(c: dict) -> str:
    failed = [ch.get("label") or ch.get("key") for ch in c.get("checks") or [] if not ch.get("passed")]
    if failed:
        return "、".join(failed)
    if c.get("reject_reason"):
        return str(c["reject_reason"])
    return "-"


def research_markdown(reg: dict) -> str:
    """run_research が保存した registry から、全候補の成績表（markdown）を作る。"""
    labels = S.get(None, "QUANT_ASSET_CLASS_LABEL")
    cands = list((reg.get("candidates") or {}).values())
    n = {k: sum(1 for c in cands if c.get("status") == k) for k in _STATUS_JA}
    q = reg.get("data_quality") or {}
    out = [f"# 検証ラボの成績表（{reg.get('as_of') or '-'} 時点）", "",
           f"- 検証した戦略：{len(cands)}本（採用 {n['adopted']}・観察 {n['watch']}・不採用 {n['rejected']}）",
           f"- 試した設定の数（多重検定の割引に使う）：{reg.get('n_trials', '-')}",
           f"- 金利のデータ：{q.get('rates_source') or '-'}"
           f"・取れなかった銘柄 {len(q.get('missing') or [])}件・価格が古い銘柄 {len(q.get('stale') or [])}件"
           f"・1日で±40%超の動き {len(q.get('jumps') or [])}件"]
    port = reg.get("portfolio") or {}
    if port.get("weights"):
        pp = (port.get("p_profit") or {}).get("252")
        out.append(f"- 採用した組み合わせ：12か月後に利益が出ている確率 {_pct(pp)}"
                   f"・年率の見込み {_pct((port.get('metrics') or {}).get('ann_return'), signed=True)}")
    else:
        out.append("- 採用した組み合わせ：なし（現金）")
    out += ["", "| 資産クラス | 名前 | 状態 | 検証の日数 | 年率 | シャープ | DSR | PBO | 12か月の利益の確率 | コスト2倍のシャープ | 最大の下げ | 落ちた項目 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in sorted(cands, key=_sort_key):
        m = c.get("metrics") or {}
        st = c.get("stress") or {}
        days = m.get("days")
        row = [labels.get(c.get("asset_class"), c.get("asset_class") or "-"), c.get("label") or c.get("id"),
               _STATUS_JA.get(c.get("status"), "-"), f"{int(days)}" if isinstance(days, (int, float)) else "-",
               _pct(m.get("ann_return"), signed=True), _num(m.get("gate_sharpe", m.get("sharpe"))), _num(m.get("dsr")),
               _pct(m.get("pbo")), _pct((m.get("p_profit") or {}).get("252")), _num(st.get("sharpe")),
               _pct(m.get("max_drawdown")), _failed_items(c)]
        out.append("| " + " | ".join(_cell(x) for x in row) + " |")
    out += ["", "シャープは理論価格の戦略では割引後の値です。" + CAUTIONS[0].lstrip("※")]
    return "\n".join(out) + "\n"
