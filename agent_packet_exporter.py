# ==============================================================
# agent_packet_exporter.py  v1.0  (Wave DEPT-1)
# 投資部審議パケット生成 — stock_analyzer → Obsidian vault
# ==============================================================
# 【役割】
#   指定 ticker について stock_analyzer の各種キャッシュ・出力を
#   読み取り専用で収集し、投資部（ブル/ベア/デビル/レポーター）の
#   審議に使う「事実パケット」1ファイルを vault の 10_受信箱/ に生成する。
#
# 【設計原則】（SPEC_investment_dept.md §1, §4-1）
#   - 読み取り専用: stock_analyzer の状態を一切変更しない
#   - 全セクションに鮮度（last_updated）を必ず付記
#     → デビルアナリストの「データ鮮度検証」の一次材料
#   - データ欠損は隠さない: 「データなし（探索パス併記）」と明示
#     → 欠損自体が審議の材料（その軸の論証は弱い、と判断できる）
#   - ファクトの正本はこのパケットのみ。各エージェントのメモは
#     本ファイルへの [[wikilink]] 参照とし数値の再掲を禁ずる
#
# 【実行】
#   python3 agent_packet_exporter.py --ticker 7203.T
#   python3 agent_packet_exporter.py --ticker AAPL --vault /path/to/vault
#
# 【設定】config.py BLOCK 39
#   INVESTMENT_DEPT_VAULT_DIR : vault ルート（B-investment）
# ==============================================================

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent


def _load_config():
    spec = importlib.util.spec_from_file_location(
        "config", str(REPO_ROOT / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)

    # ── パス再配置（run_headless.py と同じ規約） ──────────────
    # config の既定 DRIVE_BASE は Colab パスのため、ローカル実行時は
    # STOCK_ANALYZER_BASE 環境変数 → リポジトリ直下 stock_data/ の順で解決。
    import os
    base_env = os.environ.get("STOCK_ANALYZER_BASE", "")
    if base_env:
        base = Path(base_env)
    elif Path(cfg.DRIVE_BASE).exists():
        base = Path(cfg.DRIVE_BASE)
    else:
        base = REPO_ROOT / "stock_data"
    cfg.DRIVE_BASE  = base
    cfg.DATA_DIR    = base / "data"
    cfg.INSIGHT_DIR = base / "insights"
    cfg.LOG_DIR     = base / "logs"
    cfg.CACHE_DIR   = base / "cache"
    return cfg


def _mtime_str(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "不明"


def _load_json(p: Path) -> Optional[dict]:
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _no_data(label: str, path: Path) -> list[str]:
    return [f"- ⚠️ {label}: **データなし**（探索: `{path}`）",
            "  - 欠損はそれ自体が審議材料（この軸の論証は裏付けが弱い）"]


def _sec_header(title: str, freshness: str) -> list[str]:
    return ["", f"## {title}", f"*鮮度: {freshness}*", ""]


def _code_of(ticker: str) -> str:
    return ticker.split(".")[0]


# ==============================================================
# BLOCK 1: セクションコレクター（全て読み取り専用・best-effort）
# ==============================================================

def collect_screener(cfg, ticker: str) -> list[str]:
    """スクリーナー最新評価（composite/sector/value/quality/pearl 等）"""
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "screener_history.json"
    lines = _sec_header("① スクリーナー最新評価", _mtime_str(p))
    data = _load_json(p)
    if data is None:
        return lines + _no_data("screener_history", p)
    try:
        # {date: {...candidates:[...]}} 形式を新しい日付から探索
        entry, found_date = None, None
        for d in sorted(data.keys(), reverse=True):
            day = data[d] or {}
            cands = day.get("candidates", []) if isinstance(day, dict) else []
            for c in cands:
                if isinstance(c, dict) and str(c.get("ticker", "")) == ticker:
                    entry, found_date = c, d
                    break
            if entry:
                break
        if not entry:
            lines.append(f"- {ticker} は直近のスクリーナー履歴に**不在**"
                         "（通過していない＝定量網の外。潜伏Pearl側を確認）")
            return lines
        lines.append(f"- 記録日: {found_date}")
        keymap = [
            ("composite_score", "総合スコア"), ("sector_score", "業種スコア"),
            ("value_score", "バリュー"), ("quality_score", "クオリティ"),
            ("technical_score", "テクニカル"), ("pearl_score", "Pearlスコア"),
            ("pearl_tier", "Pearlティア"), ("size_tier", "Sizeティア"),
            ("strategy_label", "戦略分類"), ("conviction", "確信度"),
            ("pass_filters", "フィルター通過"), ("sector_code", "業種コード"),
            ("per", "PER"), ("pbr", "PBR"), ("roe", "ROE"),
        ]
        for k, label in keymap:
            if entry.get(k) is not None:
                lines.append(f"- {label}: {entry[k]}")
    except Exception as e:
        lines.append(f"- ⚠️ 解析エラー: {str(e)[:60]}")
    return lines


def collect_sector_context(cfg, ticker: str, sector_code: str = "") -> list[str]:
    """業種コンテキスト（score_history + dashboard）"""
    p = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "score_history.json"
    lines = _sec_header("② 業種コンテキスト", _mtime_str(p))
    data = _load_json(p)
    if data is None:
        lines += _no_data("score_history", p)
    else:
        try:
            latest = sorted(data.keys())[-1]
            day = data[latest] or {}
            if sector_code and sector_code in day:
                e = day[sector_code]
                sc = e.get("score", e) if isinstance(e, dict) else e
                lines.append(f"- {latest} 業種({sector_code})スコア: {sc}")
            ranked = []
            for code, e in day.items():
                s = e.get("score") if isinstance(e, dict) else e
                if s is not None:
                    ranked.append((code, float(s)))
            ranked.sort(key=lambda x: -x[1])
            top = " / ".join(f"{c}:{s:.0f}" for c, s in ranked[:3])
            bot = " / ".join(f"{c}:{s:.0f}" for c, s in ranked[-3:])
            lines.append(f"- 業種上位3: {top}")
            lines.append(f"- 業種下位3: {bot}")
        except Exception as e:
            lines.append(f"- ⚠️ 解析エラー: {str(e)[:60]}")
    # dashboard CSV（業種シグナル）
    dp = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "dashboard" / "dashboard_latest.csv"
    if dp.exists():
        try:
            # excel_dashboard_exporter は Excel 互換の CP932 で出力する
            rows = None
            for enc in ("utf-8", "cp932"):
                try:
                    with open(dp, encoding=enc) as f:
                        rows = list(csv.DictReader(f))
                    break
                except UnicodeDecodeError:
                    continue
            if rows is None:
                raise UnicodeDecodeError("all", b"", 0, 1, "encoding不明")
            sig_rows = [r for r in rows if r.get("signal") in ("tailwind", "headwind")]
            lines.append(f"- ダッシュボード（鮮度: {_mtime_str(dp)}）: "
                         f"追い風/逆風シグナル {len(sig_rows)}件")
            for r in sig_rows[:6]:
                lines.append(f"  - {r.get('market','')} {r.get('name_ja','')}: "
                             f"{r.get('composite_score','')}点 [{r.get('signal','')}]")
        except Exception:
            pass
    return lines


def collect_midterm(cfg, ticker: str) -> list[str]:
    """中期経営計画達成状況 [I-5]"""
    p = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "midterm_plan_cache.json"
    lines = _sec_header("③ 中期経営計画 [I-5]", _mtime_str(p))
    data = _load_json(p)
    if data is None:
        return lines + _no_data("midterm_plan_cache", p)
    plan = data.get("plans", {}).get(ticker)
    if not plan:
        lines.append(f"- {ticker} の中計データなし")
        return lines
    lines.append(f"- 計画: {plan.get('plan_name', '')} "
                 f"/ 達成スコア: {plan.get('overall_score', '')} "
                 f"/ ティア: {plan.get('achievement_tier', '')} "
                 f"/ 軌道: {plan.get('trajectory', '')}")
    for metric, r in (plan.get("achievement_rates", {}) or {}).items():
        if isinstance(r, dict):
            lines.append(f"  - {metric}: 達成率 {r.get('rate', '')} ({r.get('trajectory', '')})")
    return lines


def collect_risk_signals(cfg, ticker: str, sector_code: str = "") -> list[str]:
    """リスク側シグナル（ドリフト・PEAD・縮退モード）— ベアの一次材料"""
    lines = _sec_header("④ リスク側シグナル", "各項目に併記")
    # ドリフト
    dp = Path(getattr(cfg, "INSIGHT_DIR", "stock_data/insights")) / "drift" / "drift_history.json"
    drifts = _load_json(dp)
    if drifts is None:
        lines += _no_data("drift_history", dp)
    else:
        rel = [d for d in drifts if isinstance(d, dict)
               and (not sector_code or str(d.get("code", "")) == sector_code)][-3:]
        lines.append(f"- ドリフト履歴（鮮度: {_mtime_str(dp)}）: "
                     f"全{len(drifts)}件 / 関連業種 直近{len(rel)}件")
        for d in rel:
            lines.append(f"  - {d.get('detected_at','')[:10]} {d.get('name','')}: "
                         f"{d.get('drift_type','')} {d.get('delta','')}pt "
                         f"[{d.get('severity','')}]")
    # PEAD
    pp = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "pead_candidates.json"
    pead = _load_json(pp)
    if pead is None:
        lines += _no_data("pead_candidates", pp)
    else:
        ent = None
        for c in (pead.get("candidates", []) if isinstance(pead, dict) else []):
            if isinstance(c, dict) and str(c.get("ticker", "")) == ticker:
                ent = c
                break
        lines.append(f"- PEAD（鮮度: {_mtime_str(pp)}）: "
                     + (f"候補あり {ent}" if ent else f"{ticker} は候補外"))
    # 縮退モード
    cp = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache")) / "contraction" / "contraction_history.jsonl"
    if cp.exists():
        try:
            last = [l for l in cp.read_text(encoding="utf-8").splitlines() if l.strip()][-1]
            e = json.loads(last)
            lines.append(f"- 縮退モード（鮮度: {_mtime_str(cp)}）: "
                         f"{e.get('mode', e.get('contraction_mode', '不明'))}")
        except Exception:
            lines.append(f"- 縮退モード: 解析不能（{cp}）")
    else:
        lines += _no_data("縮退モード履歴", cp)
    return lines


def collect_alt_policy(cfg, ticker: str, sector_code: str = "") -> list[str]:
    """オルタナ・政策シグナル（求人/役員/施設/13F）— ブルの一次材料"""
    lines = _sec_header("⑤ オルタナ・政策シグナル", "各項目に併記")
    cache = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    items = [
        ("人材投資 [I-1]",  cache / "hiring" / "hiring_signal_cache.json"),
        ("役員転換 [I-3]",  cache / "executive" / "executive_transition_cache.json"),
        ("施設着工 [I-4]",  cache / "facility" / "facility_project_cache.json"),
        ("機関投資家13F",   cache / "institutional_history.json"),
        ("潜伏Pearl",       cache / "latent_pearl_cache.json"),
    ]
    for label, p in items:
        data = _load_json(p)
        if data is None:
            lines += _no_data(label, p)
            continue
        hit = ""
        try:
            blob = json.dumps(data, ensure_ascii=False)
            code = _code_of(ticker)
            if ticker in blob or (len(code) >= 4 and code in blob):
                hit = " / **当該銘柄への言及あり（本文確認推奨）**"
        except Exception:
            pass
        lines.append(f"- {label}（鮮度: {_mtime_str(p)}）: データあり{hit}")
    return lines


def collect_freshness(cfg) -> list[str]:
    """モジュール実行台帳由来の鮮度一覧（OBS-1連携・デビルの検証材料）"""
    lines = _sec_header("⑥ システム健全性・データ鮮度（デビル検証用）", "実行台帳より")
    try:
        spec = importlib.util.spec_from_file_location(
            "module_ledger", str(REPO_ROOT / "module_ledger.py"))
        ml = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ml)
        ledger = ml.load_latest_ledger(cfg)
        if not ledger:
            lines.append("- ⚠️ 実行台帳なし（OBS-1 初回実行前）。"
                         "**各セクションのファイル鮮度のみで判断すること**")
            return lines
        s = ledger.get("stats", {})
        lines.append(f"- 最終実行 {ledger.get('date','')}: "
                     f"成功{s.get('success',0)} / 失敗{s.get('error',0)} / "
                     f"スキップ{s.get('skipped',0)}")
        for a in ledger.get("alerts", []):
            lines.append(f"- 🚨 連続失敗 {a['days']}日: {a['module']} "
                         f"→ **この系統のデータに基づく論証は信頼度を下げること**")
        if ledger.get("unrecorded"):
            lines.append(f"- 👻 記録なし: {', '.join(ledger['unrecorded'][:8])}")
    except Exception as e:
        lines.append(f"- ⚠️ 台帳読込エラー: {str(e)[:60]}")
    return lines


def collect_holdings(cfg, ticker: str) -> list[str]:
    """保有状況（B4の層判定=コア/カジノ・恩株判定の一次情報）
    ★ 2026-06-12 追加: 健太の実行ガードレール統合（vault README 憲法）に伴い、
      コア層（NISA・恩株・放置大型株）の審議対象外判定に必要な保有事実を提供する。"""
    p = REPO_ROOT / "data" / "portfolio_holdings.csv"
    lines = _sec_header("⑧ 保有状況（層判定の一次情報）", _mtime_str(p))
    if not p.exists():
        return lines + _no_data("portfolio_holdings", p)
    try:
        rows = None
        for enc in ("utf-8", "cp932"):
            try:
                with open(p, encoding=enc) as f:
                    rows = list(csv.DictReader(f))
                break
            except UnicodeDecodeError:
                continue
        hit = next((r for r in (rows or [])
                    if str(r.get("ticker", "")) == ticker), None)
        if not hit:
            lines.append(f"- {ticker} は**非保有**（新規エントリー審議）")
            return lines
        lines.append(f"- **保有中**: {hit.get('quantity','')}株 "
                     f"/ 取得単価 {hit.get('avg_cost_jpy','')}円 "
                     f"/ 取得日 {hit.get('purchase_date','')}")
        if hit.get("memo"):
            lines.append(f"- 保有メモ: {hit['memo']}")
        lines.append("- ⚠️ 保有銘柄の場合、コア層（NISA・恩株・放置大型株）該当性を"
                     "レポーターが README 憲法に照らして判定すること")
    except Exception as e:
        lines.append(f"- ⚠️ 解析エラー: {str(e)[:60]}")
    return lines


def collect_existing_notes(cfg, ticker: str) -> list[str]:
    """既存 vault（銘柄メモ・投資ジャーナル）への参照"""
    lines = _sec_header("⑦ 既存ナレッジへの参照", "リンクのみ")
    vault = Path(getattr(cfg, "OBSIDIAN_VAULT_DIR", ""))
    memo = vault / "銘柄メモ" / f"{ticker}.md"
    if vault and memo.exists():
        lines.append(f"- 銘柄メモあり: `{memo}`（鮮度: {_mtime_str(memo)}）")
    else:
        lines.append("- 銘柄メモなし（初回審議）")
    jp = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / "investment_journal.json"
    j = _load_json(jp)
    if j:
        ents = [e for e in (j.get("entries", []) if isinstance(j, dict) else [])
                if isinstance(e, dict) and str(e.get("ticker", "")) == ticker]
        lines.append(f"- 投資ジャーナル過去記帳: {len(ents)}件"
                     + (f"（最新: {ents[-1].get('entry_date','')} "
                        f"{ents[-1].get('status','')}）" if ents else ""))
    else:
        lines.append("- 投資ジャーナル: データなし")
    return lines


# ==============================================================
# BLOCK 2: パケット組み立て・出力
# ==============================================================

def build_packet(cfg, ticker: str) -> str:
    now = datetime.now()
    case_id = f"{now.strftime('%Y-%m-%d')}_{ticker.replace('.', '')}"

    # screener から sector_code を先に解決（他セクションで使う）
    sector_code = ""
    sh = _load_json(Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
                    / "screener_history.json")
    if sh:
        try:
            for d in sorted(sh.keys(), reverse=True):
                for c in (sh[d] or {}).get("candidates", []):
                    if isinstance(c, dict) and str(c.get("ticker", "")) == ticker:
                        sector_code = str(c.get("sector_code", ""))
                        break
                if sector_code:
                    break
        except Exception:
            pass

    lines = [
        "---",
        f"case_id: {case_id}",
        f"ticker: {ticker}",
        f"sector_code: \"{sector_code}\"",
        f"generated_at: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "type: 審議パケット",
        "source: agent_packet_exporter.py (stock_analyzer)",
        "---",
        "",
        f"# 審議パケット: {ticker}",
        "",
        "> **本ファイルがこの審議における数値・事実の唯一の正本である。**",
        "> 各アナリストのメモは本ファイルへの参照（wikilink）で論拠を示し、",
        "> 数値の再掲は論証に必要な最小限に留めること（運用ルール原則4）。",
    ]
    lines += collect_screener(cfg, ticker)
    lines += collect_sector_context(cfg, ticker, sector_code)
    lines += collect_midterm(cfg, ticker)
    lines += collect_risk_signals(cfg, ticker, sector_code)
    lines += collect_alt_policy(cfg, ticker, sector_code)
    lines += collect_freshness(cfg)
    lines += collect_holdings(cfg, ticker)
    lines += collect_existing_notes(cfg, ticker)
    lines += [
        "",
        "---",
        "## 審議への引き継ぎ事項",
        "- 「データなし」セクションを論拠に使う場合は、その旨を明示すること",
        "- 鮮度が7日超のデータに基づく論点は、デビルが信頼度を1段階下げる",
        f"- 審議フォルダ: `20_審議/{case_id}/`（bull.md / bear.md / devil.md）",
        f"- 最終出力: `30_レポート/{case_id}_判断レポート.md`",
    ]
    return "\n".join(lines)


def export(ticker: str, vault: str = "") -> Path:
    cfg = _load_config()
    vault_dir = Path(vault or getattr(cfg, "INVESTMENT_DEPT_VAULT_DIR", ""))
    if not str(vault_dir) or not vault_dir.exists():
        raise SystemExit(f"❌ vault が見つかりません: {vault_dir}\n"
                         f"   --vault で指定するか config.py BLOCK 39 を確認")
    inbox = vault_dir / "10_受信箱"
    inbox.mkdir(parents=True, exist_ok=True)
    md = build_packet(cfg, ticker)
    out = inbox / f"packet_{datetime.now().strftime('%Y%m%d')}_{ticker.replace('.', '')}.md"
    out.write_text(md, encoding="utf-8")
    print(f"✅ 審議パケット生成: {out}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="投資部審議パケット生成")
    ap.add_argument("--ticker", required=True, help="例: 7203.T / AAPL")
    ap.add_argument("--vault", default="", help="vault ルート（省略時は config BLOCK 39）")
    args = ap.parse_args()
    export(args.ticker, args.vault)
