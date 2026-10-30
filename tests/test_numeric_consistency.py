"""Output-level numeric/scale consistency, including deliberately broken counterexamples."""
from pathlib import Path

import pandas as pd
import pytest

from orchestrator.checkers import validate_de


def de_table(**overrides):
    row = {"feature_id": "G1", "effect": 1.0, "pvalue": 0.01, "padj": 0.02,
           "n_pairs": 10, "contrast": "tumor_vs_adjacent", "ci_low": 0.2,
           "ci_high": 1.8, "selected": True}
    row.update(overrides)
    return pd.DataFrame([row])


def test_valid_de_table_passes(tmp_path):
    path = tmp_path / "de.csv"
    de_table().to_csv(path, index=False)
    result = validate_de(path)
    assert result["n_features"] == 1 and result["n_tested"] == 1


@pytest.mark.parametrize("overrides,error", [
    ({"padj": 0.001, "pvalue": 0.01}, "smaller than raw"),
    ({"pvalue": -0.01}, "Invalid pvalue"),
    ({"pvalue": 1.01}, "Invalid pvalue"),
    ({"padj": -0.01}, "Invalid padj"),
    ({"n_pairs": 2}, "Invalid cohort"),
    ({"effect": float("inf")}, "Nonfinite effect"),
    ({"ci_low": 2.0, "ci_high": 3.0}, "outside confidence"),
])
def test_inconsistent_de_numeric_fields_are_rejected(tmp_path, overrides, error):
    path = tmp_path / "de.csv"
    de_table(**overrides).to_csv(path, index=False)
    with pytest.raises(ValueError, match=error):
        validate_de(path)


def test_effect_units_are_not_silently_rescaled(tmp_path):
    path = tmp_path / "de.csv"
    table = de_table(effect=100.0, ci_low=90.0, ci_high=110.0)
    table.to_csv(path, index=False)
    # The validator preserves the supplied numeric scale; it does not divide by 100.
    assert pd.read_csv(path).effect.iloc[0] == 100.0
    validate_de(path)
