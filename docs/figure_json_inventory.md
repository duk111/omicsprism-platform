# Figure JSON inventory

Phase 3 inventory of structured figure artifacts. The sources inspected were the
platform DEG/DEM exporters in `backend/app/job_execution.py` and the read-only
OmicsPrism exporters under `../omicsprism/src/omicsprism/visualization/`.

All figure JSON objects share these descriptive top-level fields where applicable:
`figure_id`, `title`, `chart_type`, `interactive_page_id`, `static_files`,
`default_state`, `available_states`, and `style`. Their data payloads are not a
single schema.

| Artifact | Analysis | Data roots | Entity fields and queryable row arrays | Scalars |
|---|---|---|---|---|
| `volcano.json` | DEG, DEM | `plotly_spec.data`, `plotly_spec.all_traces` | trace objects use `name`; entity labels and coordinates are parallel `text`, `x`, `y` arrays | `default_state.log2fc_threshold`, `default_state.padj_threshold` |
| `pca.json` | GMA | `plotly_spec.datasets.<source>` | `groups` objects use `sample_id`; `samples`, `coords`, and `var_exp` are parallel arrays | individual `var_exp.<index>` values |
| `dendrogram.json` | GMA | `tree_data` | `tree_data.nodes` uses `id` and `name` | `tree_data.color_threshold`, `tree_data.n_leaves` |
| `upset.json` | GMA | `upset_data` | `sets` uses `name`; `intersections` contains evidence flags, `count`, and `support` | `upset_data.n_edges` |
| `bubble-heatmap.json` | GMA | `plotly_spec` | `plotly_spec.data` uses `gene`, `metabolite`, `module`, `spearman_rho`, and `edge_weight` | plot labels and state thresholds |
| `scatter-panels.json` | GMA | `plotly_spec` | `plotly_spec.panels` uses `id`, `entity_id`, and `metabolite_id` | rank and association statistics on each panel |
| `violin-box.json` | GMA | `plotly_spec` | `plotly_spec.features` uses `id`, `feature`, and `label`; each feature contains nested `groups` | feature rank |
| `corr-heatmap.json` | GMA | `plotly_spec.data` | heatmap traces contain parallel `x`, `y`, and `z` arrays rather than object rows | trace metadata |
| `ridge.json` | GMA | `ridge_data` | `ridge_data.ridges` uses `module` and contains nested group distributions | ridge layout scalars |
| `line-panels.json` | GMA | `plotly_spec` | `plotly_spec.pairs` uses `id`, `module`, and `metabolite` | `spearman_rho`, `abs_rho`, ranks |
| `circos.json` | GMA | `circos_data.layouts.<layout>` | layout `nodes` uses `id`/`name`; layout `edges` contains graph edges | node and edge weights |

The OmicsPrism `EXPORT_MAP` writes shared page names, so several static figure
specs merge into one JSON artifact. Correlation jobs store them below
`figure_data/`; platform DEG/DEM jobs generate `figure_data/volcano.json`. Job
artifact synchronization preserves the relative path while exposing the basename
in the result artifact list.

Phase 3 therefore uses an exact basename whitelist by analysis type and a bounded
dot-path selector. A selected scalar becomes one evidence row; a selected array of
scalars or objects becomes rows with stable positive integer `_row_id` values based
on the source array index. Nested matrices and parallel arrays remain queryable only
one explicitly selected array at a time; the reader does not infer joins or flatten
arbitrary JSON.
