# ==============================================================
# quant/instruments.py — QUANT_UNIVERSE → Instrument
# ==============================================================
# 【役割】設定 QUANT_UNIVERSE（資産クラス → 項目のリスト）を types.Instrument に変換する。
# 【同じ symbol が複数クラスにあるとき】SPY は index と options の両方にある。価格の列は symbol で1本なので
#   load() は先に出た定義だけを残し、資産クラス別に引きたいときは by_class() を使う。
# 【funding】QUANT_ASSET_CLASS_FUNDING から引く。通貨は項目の currency → fx は quote → それ以外は "USD"。
# ==============================================================
from __future__ import annotations

from quant import settings as S
from quant.types import Instrument


def _currency(asset_class: str, item: dict) -> str:
    """値段の通貨。項目の指定があればそれ、無ければ fx は quote・それ以外は USD。"""
    if item.get("currency"):
        return str(item["currency"])
    if asset_class == "fx" and item.get("quote"):
        return str(item["quote"])
    return "USD"


def _make(cfg, asset_class: str, item: dict) -> Instrument:
    funding_map = S.get(cfg, "QUANT_ASSET_CLASS_FUNDING")
    if asset_class not in funding_map:
        raise KeyError(f"QUANT_ASSET_CLASS_FUNDING に資産クラスが無い: {asset_class}")
    return Instrument(
        symbol=item["symbol"],
        asset_class=asset_class,
        name=item.get("name", item["symbol"]),
        funding=funding_map[asset_class],
        cost_bps=float(item.get("cost_bps", 0.0)),
        currency=_currency(asset_class, item),
        exec_hint=item.get("exec", ""),
        base=item.get("base"),
        quote=item.get("quote"),
        iv_symbol=item.get("iv_symbol"),
    )


def by_class(cfg) -> dict:
    """資産クラス → Instrument のリスト（universe の並び順）。同じ symbol が別クラスにあっても両方に載る。"""
    out: dict = {}
    for asset_class, items in S.get(cfg, "QUANT_UNIVERSE").items():
        out[asset_class] = [_make(cfg, asset_class, it) for it in items]
    return out


def load(cfg) -> dict:
    """symbol → Instrument。同じ symbol は先に出た定義を残す（価格の列は symbol で1本のため）。"""
    out: dict = {}
    for insts in by_class(cfg).values():
        for inst in insts:
            out.setdefault(inst.symbol, inst)
    return out


def all_symbols(cfg) -> list:
    """価格を取る symbol（重複なし・出現順）。options の iv_symbol も含める。"""
    seen: dict = {}
    for insts in by_class(cfg).values():
        for inst in insts:
            seen.setdefault(inst.symbol, None)
            if inst.iv_symbol:
                seen.setdefault(inst.iv_symbol, None)
    return list(seen)
