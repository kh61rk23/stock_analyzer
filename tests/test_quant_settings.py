"""quant/settings.py の DEFAULTS と config.py の BLOCK 82 が同じ値であること（片方だけ直す事故を止める）。"""
import pytest

import config
from quant import settings as S


@pytest.mark.parametrize("name", sorted(S.DEFAULTS))
def test_config_matches_defaults(name):
    assert hasattr(config, name), f"config.py に {name} が無い"
    assert getattr(config, name) == S.DEFAULTS[name]


def test_no_extra_quant_names_in_config():
    extra = [n for n in dir(config) if n.startswith("QUANT_") and n not in S.DEFAULTS]
    assert not extra, f"settings.DEFAULTS に無い QUANT_* が config にある: {extra}"


def test_get_rejects_unknown_name():
    with pytest.raises(KeyError):
        S.get(None, "QUANT_NO_SUCH_NAME")


def test_get_prefers_cfg():
    class C:
        QUANT_WFO_FOLDS = 3
    assert S.get(C, "QUANT_WFO_FOLDS") == 3
    assert S.get(None, "QUANT_WFO_FOLDS") == S.DEFAULTS["QUANT_WFO_FOLDS"]


def test_gate_critical_keys_are_known_checks():
    known = {"oos_days", "oos_sharpe", "dsr", "pbo", "adj_p", "p_profit_12m", "stress_sharpe",
             "max_drawdown", "fold_positive"}
    assert set(S.DEFAULTS["QUANT_GATE_CRITICAL"]) <= known
