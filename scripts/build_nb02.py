"""Generate the V2 wallet-analysis notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 02 — Lire un portefeuille à partir des transactions V2\n\n"
            "Cette vue reconstitue des flux signés et un inventaire. Une valeur marquée au "
            "dernier prix est une **estimation**, pas un PnL réalisé."
        ),
        nbf.v4.new_code_cell(
            "import polars as pl\n\n"
            "from poly_data.analysis.positions import expand_to_positions\n"
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context()\n"
            "store = ParquetStore(ctx.root)\n"
            "SOURCES = ['trades', 'markets_current', 'market_outcomes']\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision})\n"
            "source_inventory(store, SOURCES)"
        ),
        nbf.v4.new_markdown_cell(
            "## Flux de trésorerie et inventaire\n\n"
            "Chaque transaction est développée côté maker et taker. Un achat dépense du cash "
            "et ajoute des jetons ; une vente fait l'inverse. Nous choisissons ici un portefeuille "
            "déterministe de la fixture, puis agrégeons ses positions."
        ),
        nbf.v4.new_code_cell(
            "positions = expand_to_positions(store.scan('trades'), player_side='both').collect()\n"
            "wallet = positions.select('player').unique().sort('player').item(0, 0)\n"
            "wallet_positions = (\n"
            "    positions.filter(pl.col('player') == wallet)\n"
            "    .group_by(['market_id', 'token_side'])\n"
            "    .agg([\n"
            "        pl.col('signed_tokens').sum().alias('net_tokens'),\n"
            "        pl.col('signed_usd').sum().alias('net_cash_flow'),\n"
            "        pl.col('timestamp').max().alias('last_trade_at'),\n"
            "    ])\n"
            ")\n"
            "print({'wallet': wallet, 'positions': wallet_positions.height})\n"
            "wallet_positions.head(10)"
        ),
        nbf.v4.new_code_cell(
            "last_prices = (\n"
            "    store.scan('trades').sort('timestamp')\n"
            "    .group_by(['market_id', 'nonusdc_side']).last()\n"
            "    .select(['market_id', pl.col('nonusdc_side').alias('token_side'), pl.col('price').alias('mark_price')])\n"
            "    .collect()\n"
            ")\n"
            "marked = (\n"
            "    wallet_positions.join(last_prices, on=['market_id', 'token_side'], how='left')\n"
            "    .with_columns((pl.col('net_tokens') * pl.col('mark_price') + pl.col('net_cash_flow')).alias('estimated_value'))\n"
            "    .sort('estimated_value')\n"
            ")\n"
            "marked.head(10)"
        ),
        nbf.v4.new_markdown_cell(
            "## Provenance d'une résolution\n\n"
            "Les lignes ci-dessous joignent l'inventaire au résultat officiel lorsque le marché "
            "est clos. L'absence de résultat signifie que le portefeuille reste marqué, non réglé."
        ),
        nbf.v4.new_code_cell(
            "official = store.scan('market_outcomes').select(['market_id', 'winner_token', 'resolved_at']).collect()\n"
            "markets = store.scan('markets_current').select(['id', 'question']).collect()\n"
            "(\n"
            "    marked.join(official, on='market_id', how='left')\n"
            "    .join(markets, left_on='market_id', right_on='id')\n"
            "    .select(['market_id', 'question', 'token_side', 'net_tokens', 'mark_price', 'estimated_value', 'winner_token', 'resolved_at'])\n"
            "    .head(15)\n"
            ")"
        ),
    ]
    output = Path(__file__).resolve().parents[1] / "examples" / "02-v2-wallet-analysis.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
