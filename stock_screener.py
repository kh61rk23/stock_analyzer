# ==============================================================
# stock_screener.py  v1.0
# ==============================================================
# 【ステップ3: 個別株スクリーニング（PER/PBR/ROE閾値フィルタ）】
#
# 機能:
#   base_agent.py の fetch_financials() で取得済みの財務データに
#   PER/PBR/ROEの閾値フィルタを適用し、条件を満たす銘柄を抽出する。
#
# スクリーニング条件（config.pyで変更可能）:
#   PER:  0 < PER < 20   （割高除外・赤字除外）
#   PBR:  0 < PBR < 2.0  （資産価値対比で割安）
#   ROE:  ROE > 8.0%     （収益性最低ライン）
#
# 出力例（Gmailアラート・Markdownに組み込み）:
#   銘柄スクリーニング結果:
#   ✅ 6758.T ソニーG  PER:14.2 PBR:1.8 ROE:12.3%  スコア:★★★
#   ✅ 6501.T 日立     PER:18.1 PBR:1.5 ROE:10.7%  スコア:★★☆
#
# 組み込み方法:
#   stock_analyzer_main.py の sector分析後に呼び出す。
#   または base_agent.py の run_analysis() 末尾で自動実行。
# ==============================================================

from datetime import datetime
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 0: デフォルトスクリーニング条件
# ==============================================================

DEFAULT_CRITERIA = {
    "per_min":   0.1,    # PER下限（負値=赤字を除外）
    "per_max":   25.0,   # PER上限（割高除外）
    "pbr_min":   0.1,    # PBR下限（0以下は異常値）
    "pbr_max":   2.5,    # PBR上限
    "roe_min":   8.0,    # ROE下限（%）
    "mcap_min":  None,   # 時価総額下限（億円）※Noneは制限なし
    "mcap_max":  None,   # 時価総額上限（億円）
}


# ==============================================================
# BLOCK 1: スクリーニングロジック
# ==============================================================

def _get_criteria(cfg) -> dict:
    """config.py から閾値を読み込む。未設定はデフォルト値を使用。"""
    c = dict(DEFAULT_CRITERIA)
    for key in c:
        cfg_key = f"SCREEN_{key.upper()}"
        if hasattr(cfg, cfg_key):
            c[key] = getattr(cfg, cfg_key)
    return c


def _score_ticker(company: dict, criteria: dict) -> int:
    """
    銘柄の優良度スコアを計算する（0〜5点）。
    条件を満たしているだけでなく、どれだけ優れているかを評価。
    """
    score = 0
    per   = company.get("per")
    pbr   = company.get("pbr")
    roe   = company.get("roe")

    if per and 0 < per < 15:
        score += 2    # PER15未満は特に割安
    elif per and per < criteria["per_max"]:
        score += 1

    if pbr and 0 < pbr < 1.5:
        score += 2    # PBR1.5未満は特に割安
    elif pbr and pbr < criteria["pbr_max"]:
        score += 1

    if roe and roe > 15:
        score += 2    # ROE15%超は高収益
    elif roe and roe > criteria["roe_min"]:
        score += 1

    return min(score, 5)


def screen_companies(
    companies:  list,
    cfg,
    sector_name: str = "",
) -> dict:
    """
    財務データ取得済みの企業リストにスクリーニングを適用する。

    Args:
        companies:   fetch_financials() の戻り値
                     [{"ticker":str, "name":str, "per":float,
                       "pbr":float, "roe":float, "market_cap":float,
                       "fetch_ok":bool}, ...]
        cfg:         config モジュール
        sector_name: ログ・レポート用の業種名

    Returns:
        {
          "passed":  [企業dict + "screen_score", "screen_stars"],  # 条件通過
          "failed":  [企業dict + "fail_reason"],                   # 条件不通過
          "skipped": [企業dict],                                    # データ不足
          "criteria": dict,                                         # 使用条件
          "summary":  str,                                          # 1行サマリー
        }
    """
    criteria = _get_criteria(cfg)
    passed   = []
    failed   = []
    skipped  = []

    for company in companies:
        if not company.get("fetch_ok"):
            skipped.append(company)
            continue

        per = company.get("per")
        pbr = company.get("pbr")
        roe = company.get("roe")

        # データが1つも取れていない場合はスキップ
        if per is None and pbr is None and roe is None:
            skipped.append(company)
            continue

        fail_reasons = []

        # PERチェック
        if per is not None:
            if per <= criteria["per_min"]:
                fail_reasons.append(f"PER={per:.1f}（赤字/異常値）")
            elif per > criteria["per_max"]:
                fail_reasons.append(f"PER={per:.1f}（>{criteria['per_max']}）")

        # PBRチェック
        if pbr is not None:
            if pbr <= criteria["pbr_min"]:
                fail_reasons.append(f"PBR={pbr:.1f}（異常値）")
            elif pbr > criteria["pbr_max"]:
                fail_reasons.append(f"PBR={pbr:.1f}（>{criteria['pbr_max']}）")

        # ROEチェック
        if roe is not None and roe < criteria["roe_min"]:
            fail_reasons.append(f"ROE={roe:.1f}%（<{criteria['roe_min']}%）")

        # 時価総額チェック
        mcap = company.get("market_cap")
        if mcap is not None:
            if criteria["mcap_min"] and mcap < criteria["mcap_min"]:
                fail_reasons.append(f"時価総額{mcap:.0f}億（小さすぎ）")
            if criteria["mcap_max"] and mcap > criteria["mcap_max"]:
                fail_reasons.append(f"時価総額{mcap:.0f}億（大きすぎ）")

        if fail_reasons:
            c = dict(company)
            c["fail_reason"] = " / ".join(fail_reasons)
            failed.append(c)
        else:
            c = dict(company)
            c["screen_score"] = _score_ticker(company, criteria)
            stars = "★" * c["screen_score"] + "☆" * (5 - c["screen_score"])
            c["screen_stars"] = stars
            passed.append(c)

    # passedをscreen_score降順でソート
    passed.sort(key=lambda x: x.get("screen_score", 0), reverse=True)

    total  = len(companies)
    n_pass = len(passed)
    n_skip = len(skipped)
    summary = (f"{sector_name}: {n_pass}/{total}銘柄が条件通過"
               f"（スキップ{n_skip}件）")

    return {
        "passed":   passed,
        "failed":   failed,
        "skipped":  skipped,
        "criteria": criteria,
        "summary":  summary,
    }


# ==============================================================
# BLOCK 2: レポート生成
# ==============================================================

def format_screen_result(screen_result: dict, sector_name: str = "") -> str:
    """スクリーニング結果を Markdown 形式で出力する"""
    passed   = screen_result.get("passed", [])
    failed   = screen_result.get("failed", [])
    criteria = screen_result.get("criteria", {})
    lines    = []

    if sector_name:
        lines.append(f"#### 📋 {sector_name} スクリーニング結果")
    lines.append(
        f"条件: PER<{criteria.get('per_max','?')} / "
        f"PBR<{criteria.get('pbr_max','?')} / "
        f"ROE>{criteria.get('roe_min','?')}%")
    lines.append("")

    if passed:
        lines.append("**✅ 条件通過銘柄**")
        lines.append("| 銘柄 | 会社名 | PER | PBR | ROE | 評価 |")
        lines.append("|------|--------|-----|-----|-----|------|")
        for c in passed:
            per_s = f"{c['per']:.1f}" if c.get("per") else "―"
            pbr_s = f"{c['pbr']:.1f}" if c.get("pbr") else "―"
            roe_s = f"{c['roe']:.1f}%" if c.get("roe") else "―"
            name  = c.get("name", c.get("ticker", ""))[:12]
            lines.append(
                f"| `{c['ticker']}` | {name} "
                f"| {per_s} | {pbr_s} | {roe_s} | {c.get('screen_stars','―')} |")
    else:
        lines.append("条件通過銘柄なし")

    if failed:
        lines.append("")
        lines.append("**❌ 条件不通過**")
        for c in failed:
            name = c.get("name", c.get("ticker", ""))[:10]
            lines.append(f"- `{c['ticker']}` {name}: {c.get('fail_reason','')}")

    lines.append("")
    return "\n".join(lines)


def save_screen_report(all_results: list, output_dir: Path,
                       date_str: Optional[str] = None) -> Path:
    """全業種のスクリーニング結果をまとめてMarkdownファイルに保存する"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    now   = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 個別株スクリーニングレポート",
        f"**生成日時**: {now}", "",
        "---", "",
    ]

    # 全通過銘柄を集約してスコア降順で表示
    all_passed = []
    for r in all_results:
        for c in r.get("screen_result", {}).get("passed", []):
            c2 = dict(c)
            c2["_sector"] = r.get("sector_name", "")
            all_passed.append(c2)

    all_passed.sort(key=lambda x: x.get("screen_score", 0), reverse=True)

    if all_passed:
        lines.append("## 🏆 全業種 通過銘柄ランキング")
        lines.append("")
        lines.append("| # | 銘柄 | 会社名 | 業種 | PER | PBR | ROE | 評価 |")
        lines.append("|---|------|--------|------|-----|-----|-----|------|")
        for i, c in enumerate(all_passed, 1):
            per_s = f"{c['per']:.1f}" if c.get("per") else "―"
            pbr_s = f"{c['pbr']:.1f}" if c.get("pbr") else "―"
            roe_s = f"{c['roe']:.1f}%" if c.get("roe") else "―"
            name  = c.get("name", c.get("ticker", ""))[:12]
            sec   = c.get("_sector", "")[:8]
            lines.append(
                f"| {i} | `{c['ticker']}` | {name} | {sec} "
                f"| {per_s} | {pbr_s} | {roe_s} | {c.get('screen_stars','―')} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 業種別 詳細")
    lines.append("")
    for r in all_results:
        sr   = r.get("screen_result", {})
        sname = r.get("sector_name", "")
        lines.append(format_screen_result(sr, sname))

    output_path = output_dir / f"screen_report_{date_str}.md"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  📋 スクリーニングレポート保存: {output_path}")
    except Exception as e:
        print(f"  ⚠️  スクリーニングレポート保存失敗: {e}")
    return output_path
