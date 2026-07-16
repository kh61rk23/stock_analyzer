# ==============================================================
# tests/test_api_key_health.py  (Issue J-2 / APIキー生存判定)
# 純関数のみをテストする（ネットワーク非依存・モック不要）。
# ==============================================================
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_key_health as kh


# ── HTTPコード → 状態マッピング（汎用/EDINETDB/FRED/DeepL/e-Stat）──
def test_classify_http_generic():
    assert kh.classify_http_generic(200) == kh.STATE_ALIVE
    assert kh.classify_http_generic(401) == kh.STATE_DEAD
    assert kh.classify_http_generic(403) == kh.STATE_DEAD
    assert kh.classify_http_generic(429) == kh.STATE_LIMITED
    assert kh.classify_http_generic(None) == kh.STATE_LIMITED  # 通信不能


def test_classify_edinetdb_rate_limit_is_not_dead():
    # 429（レート制限）は失効❌ではなく⚠️扱いであること
    assert kh.classify_edinetdb(200) == kh.STATE_ALIVE
    assert kh.classify_edinetdb(403) == kh.STATE_DEAD
    assert kh.classify_edinetdb(429) == kh.STATE_LIMITED


def test_classify_fred():
    assert kh.classify_fred(200) == kh.STATE_ALIVE
    # 400 かつ本文に api_key を含む場合のみ失効判定
    assert kh.classify_fred(400, "Bad Request. Invalid api_key.") == kh.STATE_DEAD
    assert kh.classify_fred(400, "some other 400 reason") == kh.STATE_LIMITED
    assert kh.classify_fred(500) == kh.STATE_LIMITED


def test_classify_deepl_quota_vs_dead():
    assert kh.classify_deepl(200) == kh.STATE_ALIVE
    assert kh.classify_deepl(403) == kh.STATE_DEAD       # 失効
    assert kh.classify_deepl(456) == kh.STATE_LIMITED    # quota超過は失効ではない


def test_classify_estat():
    assert kh.classify_estat(0, has_auth_error=False) == kh.STATE_ALIVE
    assert kh.classify_estat(1, has_auth_error=True) == kh.STATE_DEAD
    assert kh.classify_estat(None, has_auth_error=False) == kh.STATE_LIMITED


# ── キー未設定判定 ────────────────────────────────────────────
def test_is_unset():
    assert kh.is_unset(None) is True
    assert kh.is_unset("") is True
    assert kh.is_unset("   ") is True
    assert kh.is_unset("abc") is False


# ── マスキング（キー値・その一部が出力に絶対に含まれないこと）──
def test_mask_key_never_leaks_value():
    secret = "SUPER-SECRET-abcdef123456"
    masked = kh.mask_key(secret)
    assert secret not in masked
    # 値の断片（部分文字列）も含まれないこと
    assert "abcdef" not in masked
    assert "SUPER" not in masked
    assert masked == "<set:len=%d>" % len(secret)
    assert kh.mask_key("") == "<unset>"


def test_detail_and_entries_never_leak_key_value():
    # format_detail / build_summary の出力に生キーが混ざらない設計であること
    secret = "xkey-9999-should-not-appear"
    entries = {
        "EDINET_API_KEY": {"state": kh.STATE_DEAD, "message": "HTTP 401（失効の疑い）"},
        "DEEPL_API_KEY": {"state": kh.STATE_ALIVE, "message": "HTTP 200（正常）"},
    }
    summary = kh.build_summary(entries)
    detail = kh.format_detail("EDINET_API_KEY", kh.STATE_DEAD, "HTTP 401（失効の疑い）")
    assert secret not in summary
    assert secret not in detail


# ── サマリ文字列生成 ──────────────────────────────────────────
def test_build_summary_counts_and_dead_list():
    entries = {
        "A": {"state": kh.STATE_ALIVE},
        "B": {"state": kh.STATE_ALIVE},
        "EDINET_API_KEY": {"state": kh.STATE_DEAD},
        "DEEPL_API_KEY": {"state": kh.STATE_DEAD},
        "C": {"state": kh.STATE_LIMITED},
        "D": {"state": kh.STATE_SKIPPED},
        "E": {"state": kh.STATE_SKIPPED},
    }
    summary = kh.build_summary(entries)
    assert summary.startswith("🔑 [key_health]")
    assert "✅2" in summary
    assert "❌2" in summary
    assert "⚠️1" in summary
    assert "⏭️2" in summary
    # 失効キーは名前でリストされ、原因特定を即可能にする
    assert "❌: DEEPL_API_KEY, EDINET_API_KEY" in summary


def test_build_summary_no_dead():
    entries = {"A": {"state": kh.STATE_ALIVE}, "B": {"state": kh.STATE_SKIPPED}}
    summary = kh.build_summary(entries)
    assert "❌:" not in summary  # 失効なしのときは ❌ リストを付けない


# ── 台帳ステータス（❌が1件でもあれば warn）──────────────────
def test_ledger_status():
    assert kh.ledger_status_of({"A": {"state": kh.STATE_ALIVE}}) == "success"
    assert kh.ledger_status_of(
        {"A": {"state": kh.STATE_ALIVE}, "B": {"state": kh.STATE_DEAD}}
    ) == "warn"


# ── 前営業日（土日スキップ）─────────────────────────────────
def test_prev_business_day_skips_weekend():
    # 2026-07-20 は月曜 → 前営業日は 2026-07-17（金）
    assert kh.prev_business_day(_dt.date(2026, 7, 20)) == _dt.date(2026, 7, 17)
    # 2026-07-17 は金曜 → 前営業日は 2026-07-16（木）
    assert kh.prev_business_day(_dt.date(2026, 7, 17)) == _dt.date(2026, 7, 16)


# ── 未設定キーの分類（run_key_health を requests無し想定でも壊さない）──
def test_run_key_health_unset_keys_do_not_crash():
    class _Cfg:
        pass  # 全キー未設定

    result = kh.run_key_health(_Cfg())
    entries = result["entries"]
    assert entries["EDINET_API_KEY"]["state"] == kh.STATE_UNSET
    assert entries["ANTHROPIC_API_KEY"]["state"] == kh.STATE_SKIPPED
    assert entries["GOOGLE_OAUTH"]["state"] == kh.STATE_SKIPPED
    assert result["ledger_status"] == "success"  # 未設定は❌ではない
    assert result["summary"].startswith("🔑 [key_health]")
