# ==============================================================
# rental_property_ingestor.py
# 有報の注記「賃貸等不動産関係」（貸借対照表計上額・期末時価）を EdinetDB から取る
# ==============================================================
# 【取得元】
#   EdinetDB REST `GET /v1/companies/{edinet_code}/real-estate`（1社1リクエスト）。
#   応答は bs（土地・建物・投資不動産）／lease_disclosure（賃貸等不動産の簿価・期末時価）／
#   land_price_benchmark／facilities のブロックを持つ。期末時価は会社が算定・鑑定した値で市場価格ではない。
#   EdinetDB 側で時価÷簿価が 0.1 未満・30 超、15兆円超の値は NULL にされている。
# 【いまの段階】
#   応答の列名・年度の持ち方（最新年度だけか時系列か）・単位の表し方が公開資料に書かれていないため、
#   まず --probe で1社分の生の応答を保存し、それを見てから BQ への写像を書く（推測で列を決めない）。
# 【予算】
#   EdinetDB の日次枠（EDINETDB_DAILY_BUDGET）を日次取り込みと共有する。DBRestClient の台帳と
#   送信間隔の制御をそのまま使い、枠切れ（last_error="budget"）なら何もせず失敗で返す。
# 【実行】 python3 rental_property_ingestor.py --probe E03856
#   日次処理は probe_once を EdinetDB 取り込みの直前に呼び、応答の形を保存するまで1日1回だけ試す。
# ==============================================================
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def _client(cfg):
    from db_rest_client import DBRestClient
    api_key = str(getattr(cfg, "EDINETDB_API_KEY", "") or "").strip()
    if not api_key:
        return None
    data_dir = Path(getattr(cfg, "DATA_DIR", "stock_data/data"))
    return DBRestClient(
        getattr(cfg, "EDINETDB_BASE_URL", "https://edinetdb.jp/v1"), api_key,
        data_dir / "edinetdb_cache", int(getattr(cfg, "EDINETDB_DAILY_BUDGET", 97)), "edinetdb",
    )


def probe(cfg, edinet_code: str) -> int:
    """1社分の生の応答を stock_data/data/edinetdb_real_estate_probe_<code>.json に保存する。"""
    client = _client(cfg)
    if client is None:
        print("❌ EDINETDB_API_KEY がない")
        return 1
    data = client.get(f"/companies/{edinet_code}/real-estate")
    if data is None:
        print(f"❌ 取得できない（last_error={getattr(client, 'last_error', None)}）")
        return 1
    out = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / f"edinetdb_real_estate_probe_{edinet_code}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"fetched": date.today().isoformat(), "edinet_code": edinet_code, "data": data},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
    print(f"✅ 保存: {out}（最上位キー: {keys}）")
    print(json.dumps(data, ensure_ascii=False)[:3000])
    return 0


PROBE_CODE = "E03856"


def probe_once(cfg) -> str:
    """応答の形を保存したファイルが無ければ1社分だけ試し取りする（日次処理の EdinetDB 取り込みの直前に呼ぶ）。

    EdinetDB の日次枠は日次取り込みで使い切るため、手動実行では枠が残っていないことが多い。
    保存できた日以降は何もしない（枠を使わない）。
    """
    out = Path(getattr(cfg, "DATA_DIR", "stock_data/data")) / f"edinetdb_real_estate_probe_{PROBE_CODE}.json"
    if out.exists():
        return "exists"
    return "saved" if probe(cfg, PROBE_CODE) == 0 else "failed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="EDINET_CODE", required=True, help="1社分の生の応答を保存する")
    args = ap.parse_args(argv)
    import config
    return probe(config, args.probe)


if __name__ == "__main__":
    sys.exit(main())
