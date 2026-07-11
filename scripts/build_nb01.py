"""Generate the V2 lake discovery notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 01 — Découvrir les événements et les résolutions V2\n\n"
            "Objectif : suivre un événement brut jusqu'au marché, puis distinguer une issue "
            "officielle d'un simple prix observé."
        ),
        nbf.v4.new_code_cell(
            "import polars as pl\n\n"
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context()\n"
            "store = ParquetStore(ctx.root)\n"
            "SOURCES = ['order_filled_v2', 'trades', 'markets_current', 'market_assets', 'market_outcomes']\n"
            "missing = [source for source in SOURCES if not list((ctx.root / source).rglob('*.parquet'))]\n"
            "if missing:\n"
            "    raise FileNotFoundError(\n"
            "        f'Missing sources: {missing}. Build the fixture with: '\n"
            "        'uv run python scripts/make_synthetic_smoke_fixture.py'\n"
            "    )\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision})\n"
            "source_inventory(store, SOURCES)"
        ),
        nbf.v4.new_markdown_cell(
            "## Provenance d'un événement\n\n"
            "Chaque ligne brute a un bloc, une transaction, un ordre et un actif. L'étape silver "
            "ne devine pas son marché : elle passe par la dimension ``market_assets``."
        ),
        nbf.v4.new_code_cell(
            "raw = store.scan('order_filled_v2')\n"
            "raw.select([\n"
            "    pl.len().alias('events'),\n"
            "    pl.col('block_number').min().alias('first_block'),\n"
            "    pl.col('block_number').max().alias('last_block'),\n"
            "    pl.col('asset').n_unique().alias('assets'),\n"
            "]).collect()"
        ),
        nbf.v4.new_code_cell(
            "events = store.scan('order_filled_v2').select(['id', 'timestamp', 'asset', 'price', 'side'])\n"
            "assets = store.scan('market_assets').select(['asset', 'market_id', 'token_side'])\n"
            "events.join(assets, on='asset', how='left').head(10).collect()"
        ),
        nbf.v4.new_markdown_cell(
            "## Résultats officiels versus proxys de prix\n\n"
            "Un dernier prix proche de 0 ou 1 peut être intéressant à explorer, mais ce n'est pas "
            "une résolution. Les métriques supervisées et les PnL réalisés ci-dessous utilisent "
            "uniquement ``market_outcomes``. Les marchés sans ligne dans cette table restent "
            "censurés."
        ),
        nbf.v4.new_code_cell(
            "trades = store.scan('trades').select(['market_id', 'timestamp', 'price', 'nonusdc_side'])\n"
            "official = store.scan('market_outcomes').select(['market_id', 'winner_token', 'resolved_at'])\n"
            "last_prices = (\n"
            "    trades.sort('timestamp').group_by('market_id').last()\n"
            "    .select(['market_id', pl.col('price').alias('last_observed_price')])\n"
            ")\n"
            "(\n"
            "    official.join(last_prices, on='market_id', how='left')\n"
            "    .join(store.scan('markets_current').select(['id', 'question']), left_on='market_id', right_on='id')\n"
            "    .sort('resolved_at').head(10).collect()\n"
            ")"
        ),
        nbf.v4.new_markdown_cell(
            "La colonne ``winner_token`` est la référence finale. ``last_observed_price`` reste "
            "un diagnostic : elle ne doit pas être transformée en étiquette pour évaluer un modèle."
        ),
    ]
    for index, cell in enumerate(notebook.cells):
        cell["id"] = f"01-{index:02d}"
    output = Path(__file__).resolve().parents[1] / "examples" / "01-v2-lake-discovery.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
