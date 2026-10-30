# Scientific benchmark

`scripts/90_scientific_benchmark.py` runs deterministic guard benchmarks over
synthetic evidence plus a real TCGA-LIHC structural check. It does **not** call a
model, a literature connector or the network.

```sh
python scripts/90_scientific_benchmark.py --output reports/scientific_benchmark
```

Outputs:

- `reports/scientific_benchmark/scientific_benchmark.json`
- `reports/scientific_benchmark/scientific_benchmark.md`

## What is covered

| Case | What it tests |
|---|---|
| `selection_thresholds` | Frozen `adjusted_p/effect/status/n` rule; inconsistent `selected` is rejected, not relabelled |
| `multiomics_measured_vs_derived` | Two measured assays are counted; pathway enrichment is derived, not a second measured omics |
| `literature_support_and_counterevidence` | Exact quotes for both support and contradiction against a frozen source |
| `full_registry_counterevidence` | Evidence beyond the 120-row display is retrievable; Critic must review every counterevidence candidate, omission fails |
| `experiment_and_causal_boundary` | Flux/enzyme-activity/cell-of-origin/causal overreach rejected without a same-clause hedge; experiment schema includes controls, readouts, replication and falsification fields |
| `real_tcga_lihc_structure` | Real TCGA-LIHC paired metadata: 100 samples, 50 tumor/non-tumor pairs, recorded provenance |

## Status vocabulary

- `engineering_status`: `passed` means the deterministic synthetic guard cases
  behaved as specified. It is not a scientific result.
- `scientific_validation_status`: always `NOT_EVALUABLE` in this harness. Real
  model reasoning, real multi-omics inference, live literature counterevidence
  retrieval and human scientific review require separate authorized runs.
- Real TCGA-LIHC differential expression, real multi-omics integration and real
  model reasoning are listed as `NOT_EVALUABLE` with the missing prerequisite.

## Reporting boundary

Passing unit tests or this benchmark does not establish that a biological
conclusion is correct, that a model's reasoning is scientifically valid, or that
an experiment will replicate. Those claims require real external inputs,
authorized model runs and independent scientific review. Do not upgrade the
project's `scientific_status` from `NOT_EVALUABLE` based on this file.
