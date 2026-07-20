# ==============================================================
# segment_signal.py  v1.0  (Wave 4 - E3)
# ==============================================================
# 【役割】
#   BigQuery taxonomy.segments_master からセグメント別収益性を取得し、
#   業種単位の「セグメントシグナルスコア」を計算する。
#   base_agent.py の calc_composite_score() に edinet_segment_bonus
#   として加算される（最大 +7pt / 最小 -5pt）。
#
# 【スコア計算ロジック】
#   3指標の合算：
#   (A) 主力セグメント成長率スコア     : -3〜+3 pt
#   (B) セグメント集中度 HHI スコア    : -1〜+2 pt
#   (C) 業種平均比較スコア             : -1〜+2 pt
#   合計をクランプ: [SEGMENT_BONUS_MIN(-5), SEGMENT_BONUS_MAX(+7)]
#
# 【数式】
#   $HHI = \sum_{i} s_i^2$ （s_i = セグメント i の売上構成比）
#   $growth\_score = tanh(growth\_rate \times 5) \times 3$
#   $hhi\_score = 2 \times (1 - HHI) - 1$（低集中ほど高スコア）
#
# 【BQ テーブル参照】
#   taxonomy.segments_master
#     segment_raw_name  STRING
#     sector_code       STRING   33業種コード
#     sector_name_ja    STRING
#     similarity_score  FLOAT64
#     mapped_by         STRING
#     verified_flag     BOOL
#     review_queue      BOOL
#     created_at        TIMESTAMP
#     updated_at        TIMESTAMP
#
# 【呼び出し方】
#   from segment_signal import calc_segment_signal
#   bonus = calc_segment_signal(ticker="6758.T", cfg=cfg)
#   # -> float  例: +3.5
#
# 【依存】
#   config.py (BLOCK 35: SEGMENT_BONUS_MAX / SEGMENT_BONUS_MIN)
#   edinet_bq_client.py (BQ 読み込み)
#   segment_mapper.py   (セグメント名→業種コードのマッピング)
#
# 【フォールバック設計】
#   BQ 未接続・テーブル未存在・データ不足 → すべて 0.0 を返す。
#   システム全停止は絶対に起こさない。
# ==============================================================

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ==============================================================
# BLOCK 1: BQ からセグメントデータを取得
# ==============================================================

def _fetch_segment_data_from_bq(ticker: str, cfg) -> list[dict]:
    """
    edinet_bq_client 経由で taxonomy.segments_master から
    ticker に紐づくセグメントデータを取得する。

    Returns:
        [
          {
            "segment_raw_name": str,
            "sector_code":      str,
            "sector_name_ja":   str,
            "similarity_score": float,
            "verified_flag":    bool,
            "revenue_share":    float,   # 売上構成比 (0〜1)
            "growth_rate":      float,   # セグメント売上成長率 (YoY)
            "op_margin":        float,   # セグメント営業利益率
          },
          ...
        ]
        [] : 取得失敗時
    """
    # 2026-07-19是正: 旧コードは (a) 存在しない bq.get_segment_data / bq.query を
    # 参照して常に空リストを返し（幻のAPI）、(b) フォールバックSQLも実在しない
    # テーブル（taxonomy.segments_master / edinet.segment_financials）を参照していた。
    # 実テーブルは edinet.segments × edinet.segment_finance（MCP直queryで確認済み）。
    # EDINETBQClient.get_segment_finance（2026-07-19実装）→ build_segment_view で
    # 本モジュールの期待スキーマへ変換する。
    try:
        import sys as _sys, importlib as _imp, pathlib as _pl
        _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
        bq_mod = _imp.import_module("edinet_bq_client")
        bq = bq_mod.EDINETBQClient()
        raw_rows = bq.get_segment_finance(ticker)
        return build_segment_view(raw_rows)
    except Exception as e:
        print(
            f"    ⚠️  [segment_signal] BQ セグメントデータ取得失敗 "
            f"({ticker}): {str(e)[:80]}"
        )
        return []


def build_segment_view(raw_rows: list[dict]) -> list[dict]:
    """
    get_segment_finance の生行（全期間）を本モジュールの期待スキーマ
    （最新期1セグメント1行＋growth_rate）へ変換する純粋関数。

    growth_rate は同名セグメント（segment_name_norm キー）の
    最新期 revenue と直前期 revenue から計算する:
      $growth\\_rate = (rev_{latest} - rev_{prev}) / |rev_{prev}|$
    直前期がない・revenue が欠損 → None（スコア側で中立 0.0 扱い）。

    verified_flag は True 固定（EDINET XBRL の実数値由来であり、
    taxonomy マッピングの推定値ではないため信頼度割引の対象にしない）。
    """
    if not raw_rows:
        return []

    periods = sorted(
        {str(r.get("report_period")) for r in raw_rows if r.get("report_period")},
        reverse=True,
    )
    if not periods:
        return []
    latest = periods[0]
    prev   = periods[1] if len(periods) > 1 else None

    def _key(r):
        return str(r.get("segment_name_norm") or r.get("segment_name") or "")

    prev_rev = {}
    if prev:
        for r in raw_rows:
            if str(r.get("report_period")) == prev and r.get("revenue") is not None:
                prev_rev[_key(r)] = float(r["revenue"])

    out = []
    for r in raw_rows:
        if str(r.get("report_period")) != latest:
            continue
        growth = None
        rev = r.get("revenue")
        pv  = prev_rev.get(_key(r))
        if rev is not None and pv is not None and pv != 0:
            growth = round((float(rev) - pv) / abs(pv), 6)
        out.append({
            "segment_raw_name": r.get("segment_name"),
            "revenue_share":    r.get("revenue_ratio"),
            "op_margin":        r.get("operating_margin"),
            "growth_rate":      growth,
            "verified_flag":    True,
        })
    return out


# ==============================================================
# BLOCK 2: ローカルキャッシュ管理
# ==============================================================
# BQ コール削減のため、取得結果を TTL 付きでローカルキャッシュする。
# キャッシュパス: CACHE_DIR/segment/segment_signal_{ticker}.json
# TTL: SEGMENT_MAPPER_TTL_DAYS (90日) を流用

def _get_signal_cache_path(ticker: str, cfg) -> Path:
    base = Path(getattr(cfg, "CACHE_DIR", "stock_data/cache"))
    d = base / "segment"
    d.mkdir(parents=True, exist_ok=True)
    safe_ticker = ticker.replace(".", "_").replace("/", "_")
    return d / f"segment_signal_{safe_ticker}.json"


def _load_signal_cache(ticker: str, cfg) -> Optional[dict]:
    """TTL 内のキャッシュがあれば返す。なければ None。"""
    path = _get_signal_cache_path(ticker, cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ttl_days = getattr(cfg, "SEGMENT_MAPPER_TTL_DAYS", 90)
        cached_at_str = data.get("cached_at", "")
        if not cached_at_str:
            return None
        cached_at = datetime.fromisoformat(cached_at_str)
        if datetime.now() - cached_at > timedelta(days=ttl_days):
            return None  # TTL 切れ
        return data
    except Exception:
        return None


def _save_signal_cache(ticker: str, cfg, payload: dict) -> None:
    """キャッシュ保存。失敗は警告のみ。"""
    path = _get_signal_cache_path(ticker, cfg)
    try:
        payload["cached_at"] = datetime.now().isoformat()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"    ⚠️  [segment_signal] キャッシュ保存失敗 ({ticker}): {e}")


# ==============================================================
# BLOCK 3: スコア計算ロジック
# ==============================================================

def _calc_growth_score(segments: list[dict]) -> float:
    """
    主力セグメント（revenue_share 最大）の成長率からスコアを計算する。

    $growth\\_score = tanh(growth\\_rate \times 5) \times 3$

    growth_rate が取れない場合は中間値 (0.0) を返す。
    範囲: [-3.0, +3.0]
    """
    if not segments:
        return 0.0

    # revenue_share の降順でソートして主力セグメントを特定
    sorted_segs = sorted(
        [s for s in segments if s.get("revenue_share") is not None],
        key=lambda x: float(x.get("revenue_share", 0)),
        reverse=True,
    )

    if not sorted_segs:
        return 0.0

    main_seg     = sorted_segs[0]
    growth_rate  = main_seg.get("growth_rate")

    if growth_rate is None:
        return 0.0

    try:
        # tanh 変換: growth_rate=+20% → score≈+2.5, -20% → score≈-2.5
        score = math.tanh(float(growth_rate) * 5.0) * 3.0
        return round(score, 3)
    except Exception:
        return 0.0


def _calc_hhi_score(segments: list[dict]) -> float:
    """
    セグメント集中度 HHI からスコアを計算する。

    $HHI = \\sum_{i} s_i^2$
    $hhi\\_score = 2 \times (1 - HHI) - 1$

    HHI=0.0（完全分散）→ +1.0
    HHI=0.5（中程度）  →  0.0
    HHI=1.0（完全集中）→ -1.0

    revenue_share の合計が 0 or データなし → 0.0 を返す。
    範囲: [-1.0, +2.0]（追加クランプ）
    """
    shares = []
    for s in segments:
        rs = s.get("revenue_share")
        if rs is not None:
            try:
                shares.append(float(rs))
            except Exception:
                pass

    if not shares or sum(shares) == 0:
        return 0.0

    # 構成比の正規化（合計が 1.0 になるよう）
    total   = sum(shares)
    normed  = [v / total for v in shares]
    hhi     = sum(v ** 2 for v in normed)

    # $hhi\_score = 2 \times (1 - HHI) - 1$
    raw_score = 2.0 * (1.0 - hhi) - 1.0
    # クランプ: [-1.0, +2.0]
    clamped = max(-1.0, min(2.0, raw_score))
    return round(clamped, 3)


def _calc_sector_avg_score(segments: list[dict], sector_code: str, cfg) -> float:
    """
    業種平均 op_margin との比較スコアを計算する。

    JAPAN_SECTORS[sector_code]["avg_op_margin"] と比較:
      主力セグメントの op_margin > 業種平均 + 5pt  → +2.0
      主力セグメントの op_margin > 業種平均         → +1.0
      主力セグメントの op_margin < 業種平均 - 5pt  → -1.0
      それ以外                                      →  0.0

    業種平均データなし・op_margin なし → 0.0
    範囲: [-1.0, +2.0]
    """
    # 主力セグメントの op_margin を取得
    sorted_segs = sorted(
        [s for s in segments if s.get("revenue_share") is not None],
        key=lambda x: float(x.get("revenue_share", 0)),
        reverse=True,
    )
    if not sorted_segs:
        return 0.0

    main_opm = sorted_segs[0].get("op_margin")
    if main_opm is None:
        return 0.0

    try:
        main_opm = float(main_opm)
    except Exception:
        return 0.0

    # 業種平均 op_margin を config から取得
    japan_sectors = getattr(cfg, "JAPAN_SECTORS", {})
    sector_info   = japan_sectors.get(sector_code, {})
    avg_opm       = sector_info.get("avg_op_margin")

    if avg_opm is None:
        # JAPAN_SECTORS に avg_op_margin が未定義 → 全業種の公開平均値（約6%）を代替
        avg_opm = 0.06

    try:
        avg_opm = float(avg_opm)
    except Exception:
        return 0.0

    diff = main_opm - avg_opm
    if diff >= 0.05:    return 2.0   # 業種平均 +5pt 超過
    elif diff >= 0.0:   return 1.0   # 業種平均超過
    elif diff <= -0.05: return -1.0  # 業種平均 -5pt 下回る
    else:               return 0.0


# ==============================================================
# BLOCK 4: メイン計算関数
# ==============================================================

def calc_segment_signal(
    ticker:       str,
    cfg,
    sector_code:  str = "",
    use_cache:    bool = True,
) -> float:
    """
    ticker のセグメントシグナルスコアを計算して返す。

    Args:
        ticker:      銘柄コード (例: "6758.T")
        cfg:         config モジュール
        sector_code: 33業種コード（空文字の場合は BQ データから取得）
        use_cache:   True のとき TTL 内のキャッシュを使用

    Returns:
        float: セグメントシグナルスコア
               [SEGMENT_BONUS_MIN, SEGMENT_BONUS_MAX] にクランプ済み
               失敗時は 0.0
    """
    try:
        # ── キャッシュ確認 ─────────────────────────────────────
        if use_cache:
            cached = _load_signal_cache(ticker, cfg)
            if cached is not None:
                score = cached.get("segment_signal_score", 0.0)
                print(
                    f"    📦 [segment_signal] {ticker}: "
                    f"キャッシュヒット → {score:+.2f}pt"
                )
                return float(score)

        # ── BQ からセグメントデータ取得 ───────────────────────
        segments = _fetch_segment_data_from_bq(ticker, cfg)

        if not segments:
            print(
                f"    ⚠️  [segment_signal] {ticker}: "
                f"セグメントデータなし → 0.0pt"
            )
            return 0.0

        # ── セクターコード解決 ────────────────────────────────
        if not sector_code:
            # BQ データからセクターコードを推定（最多出現コードを採用）
            codes = [s.get("sector_code", "") for s in segments if s.get("sector_code")]
            if codes:
                from collections import Counter
                sector_code = Counter(codes).most_common(1)[0][0]

        # ── 3指標スコア計算 ───────────────────────────────────
        growth_score = _calc_growth_score(segments)
        hhi_score    = _calc_hhi_score(segments)
        avg_score    = _calc_sector_avg_score(segments, sector_code, cfg)

        raw_total = growth_score + hhi_score + avg_score

        # ── クランプ ──────────────────────────────────────────
        bonus_max = float(getattr(cfg, "SEGMENT_BONUS_MAX", 7.0))
        bonus_min = float(getattr(cfg, "SEGMENT_BONUS_MIN", -5.0))
        final_score = round(max(bonus_min, min(bonus_max, raw_total)), 2)

        # ── 検証済みセグメント比率による信頼度割り引き ────────
        # verified_flag=True のセグメントが少ない場合はスコアを縮退
        # 全セグメントの 50% 未満しか検証済みでない場合は 50% に割り引き
        total_segs    = len(segments)
        verified_segs = sum(1 for s in segments if s.get("verified_flag"))
        if total_segs > 0:
            verified_ratio = verified_segs / total_segs
            if verified_ratio < 0.5:
                original_score = final_score
                final_score    = round(final_score * 0.5, 2)
                print(
                    f"    🔍 [segment_signal] {ticker}: "
                    f"検証済み比率 {verified_ratio*100:.0f}% < 50% → "
                    f"スコア縮退 {original_score:+.2f} → {final_score:+.2f}pt"
                )

        print(
            f"    📊 [segment_signal] {ticker}: "
            f"growth={growth_score:+.2f} "
            f"HHI={hhi_score:+.2f} "
            f"avg={avg_score:+.2f} "
            f"→ 合計 {final_score:+.2f}pt"
            f" (セグメント数={total_segs}, 検証済={verified_segs})"
        )

        # ── キャッシュ保存 ────────────────────────────────────
        _save_signal_cache(ticker, cfg, {
            "ticker":               ticker,
            "sector_code":          sector_code,
            "segment_count":        total_segs,
            "verified_count":       verified_segs,
            "growth_score":         growth_score,
            "hhi_score":            hhi_score,
            "avg_score":            avg_score,
            "segment_signal_score": final_score,
            "segments_summary": [
                {
                    "segment_raw_name": s.get("segment_raw_name", ""),
                    "sector_code":      s.get("sector_code", ""),
                    "revenue_share":    s.get("revenue_share"),
                    "growth_rate":      s.get("growth_rate"),
                    "op_margin":        s.get("op_margin"),
                    "verified_flag":    s.get("verified_flag", False),
                }
                for s in segments
            ],
        })

        return final_score

    except Exception as e:
        print(
            f"    ⚠️  [segment_signal] calc_segment_signal エラー "
            f"({ticker}): {str(e)[:80]}"
        )
        return 0.0


# ==============================================================
# BLOCK 5: 業種単位での一括計算
# ==============================================================

def calc_sector_segment_bonus(
    sector_code:  str,
    companies:    list[dict],
    cfg,
    max_tickers:  int = 5,
) -> float:
    """
    業種（sector）単位で複数銘柄のセグメントスコアを計算し、
    業種平均を返す。base_agent.build_result() から呼び出す。

    Args:
        sector_code: 33業種コード
        companies:   base_agent の companies リスト
                     （fetch_ok=True の銘柄を対象）
        cfg:         config モジュール
        max_tickers: BQ 負荷制限のため処理する銘柄の上限数

    Returns:
        float: 業種平均セグメントシグナルスコア。
               データなし・失敗時は 0.0
    """
    try:
        # fetch_ok の銘柄のみ対象（最大 max_tickers 社）
        ok_tickers = [
            str(c.get("ticker") or c.get("code") or "")
            for c in companies
            if c.get("fetch_ok") and (c.get("ticker") or c.get("code"))
        ][:max_tickers]

        if not ok_tickers:
            return 0.0

        scores = []
        for ticker in ok_tickers:
            try:
                s = calc_segment_signal(
                    ticker      = ticker,
                    cfg         = cfg,
                    sector_code = sector_code,
                    use_cache   = True,
                )
                scores.append(s)
            except Exception as e:
                print(
                    f"    ⚠️  [segment_signal] {ticker} スコア計算エラー "
                    f"（スキップ）: {str(e)[:60]}"
                )

        if not scores:
            return 0.0

        avg = round(sum(scores) / len(scores), 2)
        print(
            f"    📊 [segment_signal] {sector_code} 業種平均: "
            f"{avg:+.2f}pt ({len(scores)}/{len(ok_tickers)}社)"
        )
        return avg

    except Exception as e:
        print(
            f"    ⚠️  [segment_signal] calc_sector_segment_bonus エラー "
            f"({sector_code}): {str(e)[:80]}"
        )
        return 0.0


# ==============================================================
# BLOCK 6: 単体テスト
# ==============================================================

if __name__ == "__main__":
    import importlib.util

    cfg = None
    for p in ["/content/config.py", "config.py"]:
        if Path(p).exists():
            spec = importlib.util.spec_from_file_location("config", p)
            if spec and spec.loader:
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
                break

    if cfg is None:
        print("ERROR: config.py が見つかりません")
        raise SystemExit(1)

    print("=== segment_signal 単体テスト ===\n")

    # ── モック BQ データで数式検証 ─────────────────────────────
    mock_segments = [
        {
            "segment_raw_name": "半導体事業",
            "sector_code":      "1016",
            "sector_name_ja":   "電気機器",
            "similarity_score": 0.93,
            "verified_flag":    True,
            "revenue_share":    0.60,   # 主力 60%
            "growth_rate":      0.18,   # +18% YoY
            "op_margin":        0.14,   # 14%
        },
        {
            "segment_raw_name": "デバイス事業",
            "sector_code":      "1016",
            "sector_name_ja":   "電気機器",
            "similarity_score": 0.88,
            "verified_flag":    True,
            "revenue_share":    0.40,   # 40%
            "growth_rate":      0.05,
            "op_margin":        0.08,
        },
    ]

    print("  ── 3指標計算テスト ──")
    g = _calc_growth_score(mock_segments)
    h = _calc_hhi_score(mock_segments)
    a = _calc_sector_avg_score(mock_segments, "1016", cfg)
    total = g + h + a
    print(f"  growth_score = {g:+.3f}")
    print(f"  hhi_score    = {h:+.3f}")
    print(f"  avg_score    = {a:+.3f}")
    print(f"  合計          = {total:+.3f}")

    bonus_max = getattr(cfg, "SEGMENT_BONUS_MAX", 7.0)
    bonus_min = getattr(cfg, "SEGMENT_BONUS_MIN", -5.0)
    clamped = max(bonus_min, min(bonus_max, total))
    print(f"  クランプ後     = {clamped:+.3f}pt")

    # HHI 計算確認
    # shares=[0.6, 0.4] → HHI=0.36+0.16=0.52 → hhi_score=2*(1-0.52)-1=−0.04
    expected_hhi = 0.6**2 + 0.4**2
    print(f"\n  HHI 確認: {expected_hhi:.4f} "
          f"(hhi_score = {2*(1-expected_hhi)-1:+.4f})")

    # growth tanh 確認
    # growth_rate=0.18 → tanh(0.18*5)*3 = tanh(0.9)*3 ≈ 0.716*3 ≈ 2.147
    expected_growth = math.tanh(0.18 * 5.0) * 3.0
    print(f"  growth tanh 確認: {expected_growth:+.4f}")

    print("\n  ✅ 単体テスト完了（BQ 接続なし・モックデータで計算検証）")