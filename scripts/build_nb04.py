"""Generate a reproducible V2 Polars-versus-DuckDB benchmark notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 04 — Benchmarks reproductibles du lac V2\n\n"
            "Chaque comparaison répète la même agrégation matérialisée trois fois et vérifie "
            "une empreinte du résultat. Une valeur plus rapide mais différente est rejetée."
        ),
        nbf.v4.new_code_cell(
            "import duckdb\n"
            "import matplotlib.pyplot as plt\n"
            "import polars as pl\n\n"
            "from poly_data.analysis.bench import repeat_benchmark\n"
            "from poly_data.benchmark import benchmark_source\n"
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context()\n"
            "store = ParquetStore(ctx.root)\n"
            "SOURCES = ['order_filled_v2', 'trades', 'market_outcomes']\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision, 'polars': pl.__version__, 'duckdb': duckdb.__version__})\n"
            "source_inventory(store, SOURCES)"
        ),
        nbf.v4.new_markdown_cell("## Coût de lecture des sources\n\nLes tailles, fichiers et temps de scan sont rapportés avant toute comparaison d'agrégation."),
        nbf.v4.new_code_cell(
            "pl.DataFrame([{'source': source, **benchmark_source(store, source)} for source in SOURCES])"
        ),
        nbf.v4.new_markdown_cell(
            "## Même agrégation froide, mêmes résultats\n\n"
            "Les deux moteurs lisent les Parquet de ``trades`` et calculent le volume et le nombre "
            "de transactions par marché et issue."
        ),
        nbf.v4.new_code_cell(
            "TRADES_GLOB = (ctx.root / 'trades' / '**' / '*.parquet').as_posix()\n\n"
            "def polars_summary():\n"
            "    return (\n"
            "        store.scan('trades').group_by(['market_id', 'nonusdc_side'])\n"
            "        .agg([pl.len().cast(pl.Int64).alias('n_trades'), pl.col('usd_amount').sum().round(8).cast(pl.Float64).alias('usd_volume')])\n"
            "        .sort(['market_id', 'nonusdc_side']).collect()\n"
            "    )\n\n"
            "def duckdb_summary():\n"
            "    query = '''\n"
            "        SELECT market_id, nonusdc_side, CAST(COUNT(*) AS BIGINT) AS n_trades,\n"
            "               CAST(ROUND(SUM(usd_amount), 8) AS DOUBLE) AS usd_volume\n"
            "        FROM read_parquet(?)\n"
            "        GROUP BY market_id, nonusdc_side\n"
            "        ORDER BY market_id, nonusdc_side\n"
            "    '''\n"
            "    return pl.from_arrow(duckdb.execute(query, [TRADES_GLOB]).arrow())\n"
        ),
        nbf.v4.new_code_cell(
            "polars_cold = repeat_benchmark('cold_end_to_end', 3, polars_summary).with_columns(pl.lit('polars').alias('engine'))\n"
            "duckdb_cold = repeat_benchmark('cold_end_to_end', 3, duckdb_summary).with_columns(pl.lit('duckdb').alias('engine'))\n"
            "assert polars_cold['result_sha256'].n_unique() == duckdb_cold['result_sha256'].n_unique() == 1\n"
            "assert polars_cold['result_sha256'][0] == duckdb_cold['result_sha256'][0]\n"
            "cold = pl.concat([polars_cold, duckdb_cold])\n"
            "cold"
        ),
        nbf.v4.new_markdown_cell(
            "## Slices matérialisés (mesure séparée)\n\n"
            "Cette mesure ne lit pas le lac : elle mesure seulement un filtre sur un résumé déjà "
            "calculé. Elle est volontairement présentée séparément du benchmark bout-en-bout."
        ),
        nbf.v4.new_code_cell(
            "polars_cached_frame = polars_summary()\n"
            "duckdb_cached_frame = duckdb_summary()\n"
            "polars_cached = repeat_benchmark('cached_summary_slice', 3, lambda: polars_cached_frame.filter(pl.col('usd_volume') > 0)).with_columns(pl.lit('polars').alias('engine'))\n"
            "duckdb_cached = repeat_benchmark('cached_summary_slice', 3, lambda: duckdb_cached_frame.filter(pl.col('usd_volume') > 0)).with_columns(pl.lit('duckdb').alias('engine'))\n"
            "assert polars_cached['result_sha256'][0] == duckdb_cached['result_sha256'][0]\n"
            "cached = pl.concat([polars_cached, duckdb_cached])\n"
            "cached"
        ),
        nbf.v4.new_code_cell(
            "summary = pl.concat([cold, cached]).group_by(['label', 'engine']).agg([\n"
            "    pl.col('seconds').min().alias('min_seconds'),\n"
            "    pl.col('seconds').median().alias('median_seconds'),\n"
            "    pl.col('seconds').max().alias('max_seconds'),\n"
            "    pl.col('peak_rss_mb').median().alias('median_peak_rss_mb'),\n"
            "    pl.col('rows_out').first(),\n"
            "]).sort(['label', 'engine'])\n"
            "summary"
        ),
        nbf.v4.new_code_cell(
            "plot = summary.to_pandas()\n"
            "ax = plot.pivot(index='label', columns='engine', values='median_seconds').plot.bar(figsize=(8, 4), ylabel='median seconds')\n"
            "ax.set_title('Cold and cached workloads are reported separately')\n"
            "plt.tight_layout()"
        ),
    ]
    output = Path(__file__).resolve().parents[1] / "examples" / "04-benchmark-polars-vs-duckdb.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
