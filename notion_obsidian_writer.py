# ==============================================================
# notion_obsidian_writer.py  v1.0  (6-4 / E-3)
# Notion API + Obsidian (Google Drive Markdown) 自動連携
# ==============================================================
# 【役割】
#   毎朝の分析結果を Notion データベースと Obsidian（GDrive経由）に
#   自動書き込みし、投資判断の記録・振り返りを可能にする。
#
# 【Notion 連携】
#   NOTION_API_KEY と NOTION_DATABASE_ID が config.py に設定されていれば:
#     - 銘柄ごとのページを自動生成（スコアカード形式）
#     - スクリーニング通過日・スコア・判断理由を記録
#     - 「買い判断」「売り判断」「ウォッチ」のステータス管理
#
# 【Obsidian 連携（Google Drive 経由）】
#   NOTION_API_KEY が未設定でも Obsidian で利用可能:
#     - Google Drive の OBSIDIAN_VAULT_DIR に Markdown を書き込む
#     - Obsidian → Google Drive 同期プラグイン（例: Remotely Save）で自動同期
#     - 投資ジャーナルとして機能（Daily Note 形式）
#
# 【出力ファイル構成（Obsidian）】
#   {OBSIDIAN_VAULT_DIR}/
#     投資ジャーナル/
#       YYYY-MM-DD.md             ← デイリーサマリー
#     銘柄メモ/
#       {ticker}.md               ← 銘柄ごとの蓄積メモ
#     スクリーニング/
#       screener_YYYY-MM-DD.md   ← その日の通過銘柄一覧
#
# 【Notion データベーススキーマ】
#   スクリーナー銘柄DB:
#     Ticker (title) / 銘柄名 (text) / 通過日 (date)
#     composite_score (number) / ステータス (select: 検討中/購入済/見送り/ウォッチ)
#     判断メモ (text) / 業種 (select) / 市場 (select)
#
# 【依存】requests / config.py / stock_data/insights/ 以下のMDファイル
# ==============================================================

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

OBSIDIAN_DIR_NAME  = "stock_analyzer_vault"   # GDrive上のVaultフォルダ名
DAILY_NOTE_SUBDIR  = "投資ジャーナル"
TICKER_NOTE_SUBDIR = "銘柄メモ"
SCREENER_SUBDIR    = "スクリーニング"
MAX_NOTION_PAGES   = 20   # 1日の最大ページ生成数（APIレート制限対策）


# ==============================================================
# BLOCK 1: Obsidian (Google Drive Markdown) 書き込み
# ==============================================================

def _get_obsidian_base(cfg) -> Optional[Path]:
    """Obsidian Vault のベースパスを取得する。"""
    # 優先順位: OBSIDIAN_VAULT_DIR → DRIVE_BASE / vault_dir
    vault = getattr(cfg, "OBSIDIAN_VAULT_DIR", None)
    if vault:
        return Path(vault)
    drive_base = getattr(cfg, "DRIVE_BASE", None)
    if drive_base:
        return Path(drive_base) / OBSIDIAN_DIR_NAME
    return None


def write_daily_note(cfg, results: dict) -> Optional[str]:
    """
    デイリーサマリーノートを Obsidian Vault に書き込む。

    Returns:
        書き込んだファイルパス（失敗時 None）
    """
    base = _get_obsidian_base(cfg)
    if not base:
        return None

    today    = datetime.now().strftime("%Y-%m-%d")
    note_dir = base / DAILY_NOTE_SUBDIR
    note_dir.mkdir(parents=True, exist_ok=True)
    path     = note_dir / f"{today}.md"

    # inference_engine の判断サマリー
    inference  = results.get("inference", {})
    phase      = inference.get("phase_label", "不明")
    action_txt = inference.get("primary_action", {}).get("action", "")
    scenarios  = inference.get("active_scenarios", [])

    # signal_aggregator の上位業種
    sa       = results.get("signal_aggregator", {})
    top_buy  = sa.get("top_buy", [])
    regime   = sa.get("market_regime", "不明")

    # screener 通過銘柄
    screener = results.get("screener", {})
    passed   = [c for c in screener.get("candidates", [])
                if c.get("pass_filters")]

    lines = [
        f"# 📅 {today} 投資日誌",
        "",
        f"## 市場概況",
        f"- 市場フェーズ: **{phase}**",
        f"- 市場レジーム: **{regime}**",
        "",
        f"## 🎯 本日の推奨アクション",
        f"> {action_txt}" if action_txt else "> シグナルなし",
        "",
    ]

    if scenarios:
        lines += ["## 📋 アクティブシナリオ", ""]
        for s in scenarios[:3]:
            conf = s.get("confidence", 0)
            lines.append(f"- **{s.get('label','')}** ({conf:.0%}) — {s.get('rationale','')[:60]}")
        lines.append("")

    if top_buy:
        lines += [f"## 📈 総合シグナル上位業種", ""]
        lines += [f"- [{c}]" for c in top_buy[:5]]
        lines.append("")

    if passed:
        lines += [
            f"## 📋 screener通過銘柄（{len(passed)}件）",
            "",
            "| 銘柄 | スコア | 業種 | メモ |",
            "|-----|-------|-----|-----|",
        ]
        for c in sorted(passed, key=lambda x: -x.get("composite_score", 0))[:10]:
            t   = c.get("ticker", "")
            sc  = c.get("composite_score", 0)
            sec = c.get("sector_name", "")
            lines.append(f"| [[{t}]] | {sc:.1f} | {sec} | |")
        lines.append("")

    # macro_snapshot
    snap = results.get("macro_snapshot", {})
    if snap:
        lines += [
            "## 🌍 マクロ指標",
            f"- USD/JPY: {snap.get('usdjpy', 'N/A')}",
            f"- 米10年債: {snap.get('us10y', 'N/A')}%",
            f"- WTI原油: {snap.get('oil_wti', 'N/A')}ドル",
            f"- VIX: {snap.get('vix', 'N/A')}",
            "",
        ]

    lines += [
        "## 📝 本日の投資メモ",
        "",
        "<!-- ここに手動メモを追加 -->",
        "",
        "---",
        f"*stock_analyzer 自動生成 — {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
    ]

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def write_ticker_notes(cfg, results: dict) -> list:
    """
    screener 通過銘柄ごとのノートを Obsidian Vault に書き込む。
    既存ノートがある場合は上部に最新スコアを追記する。

    Returns:
        書き込んだファイルパスのリスト
    """
    base = _get_obsidian_base(cfg)
    if not base:
        return []

    ticker_dir = base / TICKER_NOTE_SUBDIR
    ticker_dir.mkdir(parents=True, exist_ok=True)

    screener = results.get("screener", {})
    passed   = [c for c in screener.get("candidates", [])
                if c.get("pass_filters")]
    dcf_res  = results.get("dcf_valuation", {}).get("results", {})
    today    = datetime.now().strftime("%Y-%m-%d")
    written  = []

    for c in sorted(passed, key=lambda x: -x.get("composite_score", 0))[:MAX_NOTION_PAGES]:
        ticker = c.get("ticker", "")
        name   = c.get("name", ticker)
        score  = c.get("composite_score", 0)
        sector = c.get("sector_name", "")
        per    = c.get("per")
        pbr    = c.get("pbr")
        roe    = c.get("roe")

        dcf    = dcf_res.get(ticker, {})
        intrinsic = dcf.get("dcf", {}).get("intrinsic_per_share")
        discount  = dcf.get("discount_pct")

        fname = f"{ticker.replace('.', '_')}.md"
        path  = ticker_dir / fname

        new_entry = [
            f"## {today} スクリーニング通過",
            f"- composite_score: **{score:.1f}**",
            f"- PER: {per:.1f} / PBR: {pbr:.2f} / ROE: {roe:.1f}%" if all([per,pbr,roe]) else "",
            f"- DCF理論株価: {intrinsic:,.0f}円 ({discount:+.1f}%)" if intrinsic else "",
            "- ステータス: 検討中",
            "- 判断メモ: <!-- ここに記入 -->",
            "",
        ]

        if path.exists():
            # 既存ノートの先頭に追記
            existing = path.read_text(encoding="utf-8")
            # ヘッダー部分の後に追記
            header_end = existing.find("\n---\n")
            if header_end > 0:
                content = existing[:header_end + 5] + "\n".join(new_entry) + "\n" + existing[header_end + 5:]
            else:
                content = "\n".join(new_entry) + "\n\n" + existing
        else:
            # 新規ノート作成
            content_lines = [
                f"# {name} ({ticker})",
                f"**業種**: {sector}  **市場**: {c.get('market','')}",
                "",
                "---",
                "",
            ] + new_entry + [
                "---",
                "",
                "## 投資方針メモ",
                "<!-- 長期保有の理由・売却ラインなど -->",
                "",
                "## 関連リンク",
                f"- [Yahoo Finance](https://finance.yahoo.co.jp/quote/{ticker})",
            ]
            content = "\n".join(content_lines)

        try:
            path.write_text(content, encoding="utf-8")
            written.append(str(path))
        except Exception:
            pass

    return written


def write_screener_summary(cfg, results: dict) -> Optional[str]:
    """screener通過銘柄のサマリーMDをスクリーニングフォルダに書き込む。"""
    base = _get_obsidian_base(cfg)
    if not base:
        return None

    scr_dir = base / SCREENER_SUBDIR
    scr_dir.mkdir(parents=True, exist_ok=True)

    today    = datetime.now().strftime("%Y-%m-%d")
    path     = scr_dir / f"screener_{today}.md"
    screener = results.get("screener", {})
    passed   = [c for c in screener.get("candidates", []) if c.get("pass_filters")]

    lines = [
        f"# screener通過銘柄 — {today}",
        f"**通過件数**: {len(passed)}件",
        "",
        "| 銘柄 | スコア | 業種 | PER | PBR | ROE |",
        "|-----|-------|-----|-----|-----|-----|",
    ]
    for c in sorted(passed, key=lambda x: -x.get("composite_score", 0)):
        per = f"{c['per']:.1f}" if c.get("per") else "—"
        pbr = f"{c['pbr']:.2f}" if c.get("pbr") else "—"
        roe = f"{c['roe']*100:.1f}%" if c.get("roe") else "—"
        lines.append(
            f"| [[{c['ticker']}]] | {c.get('composite_score',0):.1f} "
            f"| {c.get('sector_name','')} | {per} | {pbr} | {roe} |")

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception:
        return None


# ==============================================================
# BLOCK 2: Notion API 連携
# ==============================================================

def _notion_headers(api_key: str) -> dict:
    return {
        "Authorization":  f"Bearer {api_key}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }


def _upsert_notion_page(
    api_key:     str,
    database_id: str,
    ticker:      str,
    candidate:   dict,
    dcf_data:    dict,
) -> bool:
    """
    Notion データベースに銘柄ページを作成（または更新）する。
    既存ページがあれば更新、なければ新規作成。
    """
    try:
        import requests

        headers = _notion_headers(api_key)
        today   = datetime.now().strftime("%Y-%m-%d")
        name    = candidate.get("name", ticker)
        score   = candidate.get("composite_score", 0)
        sector  = candidate.get("sector_name", "")
        intrinsic = dcf_data.get("dcf", {}).get("intrinsic_per_share")
        discount  = dcf_data.get("discount_pct")

        # 既存ページ検索
        search_r = requests.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=headers,
            json={"filter": {"property": "Ticker",
                             "title": {"equals": ticker}}},
            timeout=8,
        )
        existing = search_r.json().get("results", []) if search_r.ok else []

        # ページプロパティ
        properties = {
            "Ticker":          {"title": [{"text": {"content": ticker}}]},
            "銘柄名":           {"rich_text": [{"text": {"content": name[:100]}}]},
            "通過日":           {"date": {"start": today}},
            "composite_score": {"number": round(score, 1)},
            "業種":             {"select": {"name": sector[:100] if sector else "不明"}},
            "ステータス":       {"select": {"name": "検討中"}},
        }
        if intrinsic:
            properties["DCF理論株価"] = {"number": round(intrinsic, 0)}
        if discount is not None:
            properties["DCF割引率%"] = {"number": round(discount, 1)}

        if existing:
            page_id = existing[0]["id"]
            r = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": properties},
                timeout=8,
            )
        else:
            r = requests.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json={"parent":     {"database_id": database_id},
                      "properties": properties},
                timeout=8,
            )

        return r.ok

    except Exception:
        return False


def write_notion_pages(cfg, results: dict) -> dict:
    """
    Notion API に screener 通過銘柄のページを一括書き込みする。

    Returns:
        {"success": int, "failed": int, "skipped": bool}
    """
    # ★ 2026-07-05 D-09決定: Notion連携は廃止（Obsidianに一本化）。
    #   Obsidian書き込み側は本フラグの影響を受けない（Notion API呼び出しのみ停止）。
    #   再開する場合は config.py の ENABLE_NOTION_API を True に戻し、
    #   NOTION_API_KEY を GitHub Secrets に登録すること。
    if not bool(getattr(cfg, "ENABLE_NOTION_API", False)):
        return {"skipped": True, "reason": "ENABLE_NOTION_API=False（D-09: Notion廃止・Obsidian一本化）"}

    api_key  = getattr(cfg, "NOTION_API_KEY", "") or ""
    db_id    = getattr(cfg, "NOTION_DATABASE_ID", "") or ""

    if not api_key or not db_id:
        return {"skipped": True, "reason": "NOTION_API_KEY or NOTION_DATABASE_ID が未設定"}

    screener = results.get("screener", {})
    passed   = [c for c in screener.get("candidates", [])
                if c.get("pass_filters")]
    dcf_res  = results.get("dcf_valuation", {}).get("results", {})

    success = failed = 0
    for c in sorted(passed, key=lambda x: -x.get("composite_score", 0))[:MAX_NOTION_PAGES]:
        ticker   = c.get("ticker", "")
        dcf_data = dcf_res.get(ticker, {})
        ok = _upsert_notion_page(api_key, db_id, ticker, c, dcf_data)
        if ok:
            success += 1
        else:
            failed += 1

    return {"success": success, "failed": failed, "skipped": False}


# ==============================================================
# BLOCK 3: メインエントリポイント
# ==============================================================

def run_notion_obsidian_writer(cfg, results: dict) -> dict:
    """
    Notion と Obsidian（GDrive）への書き込みを実行する。

    stock_analyzer_main.py から日次で呼び出す（週次レポート直後）:
        _now = _load_local_module("notion_obsidian_writer", ...)
        now_result = _now.run_notion_obsidian_writer(cfg, results)
        results["notion_obsidian"] = now_result

    Returns:
        {
          "obsidian": {"daily": path, "tickers": [path,...], "screener": path},
          "notion":   {"success": int, "failed": int, "skipped": bool},
          "timestamp": str,
        }
    """
    print("\n  📓 Notion / Obsidian 書き込み開始...")

    # ── Obsidian（GDrive Markdown）────────────────────────────
    obsidian_result = {}

    daily_path = write_daily_note(cfg, results)
    if daily_path:
        print(f"    ✅ デイリーノート: {daily_path}")
        obsidian_result["daily"] = daily_path

    ticker_paths = write_ticker_notes(cfg, results)
    if ticker_paths:
        print(f"    ✅ 銘柄ノート: {len(ticker_paths)}件")
        obsidian_result["tickers"] = ticker_paths

    scr_path = write_screener_summary(cfg, results)
    if scr_path:
        obsidian_result["screener"] = scr_path

    # ── Notion API ───────────────────────────────────────────
    notion_result = write_notion_pages(cfg, results)
    if not notion_result.get("skipped"):
        s = notion_result.get("success", 0)
        f = notion_result.get("failed", 0)
        print(f"    ✅ Notion: {s}件成功 / {f}件失敗")
    else:
        print(f"    ⏩ Notion: {notion_result.get('reason','API未設定')}")

    obsidian_base = _get_obsidian_base(cfg)
    if not obsidian_result:
        print("    ⚠️  Obsidian書き込み先が設定されていません")
        print("         config.py に OBSIDIAN_VAULT_DIR を追加してください")

    print(f"    ✅ Notion/Obsidian 書き込み完了")

    return {
        "obsidian":  obsidian_result,
        "notion":    notion_result,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
