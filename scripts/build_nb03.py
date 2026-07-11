"""Generate the explicitly toy, next-observation backtest notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 03 — Mini-backtest à exécution différée\n\n"
            "> **Avertissement — stratégie-jouet.** Ce notebook explique la chronologie d'un "
            "backtest. Il ne constitue ni une stratégie de trading ni une estimation de rendement."
        ),
        nbf.v4.new_code_cell(
            "import polars as pl\n\n"
            "from poly_data.analysis.backtest import build_transaction_bars, simulate_next_observation_strategy\n"
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context()\n"
            "store = ParquetStore(ctx.root)\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision})\n"
            "source_inventory(store, ['trades', 'markets_current', 'market_outcomes'])"
        ),
        nbf.v4.new_markdown_cell(
            "## Construire le signal puis attendre la prochaine observation\n\n"
            "Le signal compare le dernier prix journalier à sa moyenne mobile. La fonction de "
            "simulation enregistre le signal à *t* et ne remplit qu'à *t+1*. Les frais et le "
            "glissement sont appliqués au prix de remplissage."
        ),
        nbf.v4.new_code_cell(
            "trades = store.scan('trades').collect()\n"
            "market_id = trades.select('market_id').unique().sort('market_id').item(0, 0)\n"
            "market_trades = trades.filter((pl.col('market_id') == market_id) & (pl.col('nonusdc_side') == 'token1'))\n"
            "bars = build_transaction_bars(market_trades, seconds=86_400)\n"
            "bars = (\n"
            "    bars.with_columns(pl.col('close').rolling_mean(3).alias('moving_average'))\n"
            "    .drop_nulls('moving_average')\n"
            "    .with_columns((pl.col('close') > pl.col('moving_average')).cast(pl.Int64).alias('signal'))\n"
            ")\n"
            "bars"
        ),
        nbf.v4.new_code_cell(
            "split_at = bars.select(pl.col('timestamp').quantile(0.7)).item()\n"
            "train_bars = bars.filter(pl.col('timestamp') <= split_at)\n"
            "test_bars = bars.filter(pl.col('timestamp') > split_at)\n"
            "result = simulate_next_observation_strategy(test_bars, fee_bps=10, slippage_bps=20)\n"
            "print({'market_id': market_id, 'walk_forward_rows': test_bars.height, 'turnover': result.turnover, 'max_drawdown': result.max_drawdown})\n"
            "result.fills"
        ),
        nbf.v4.new_code_cell(
            "buy_and_hold = test_bars.select((pl.col('close').last() - pl.col('close').first()).alias('one_share_price_change')).item()\n"
            "summary = result.equity.tail(1).with_columns(pl.lit(buy_and_hold).alias('buy_and_hold_price_change'))\n"
            "summary"
        ),
        nbf.v4.new_markdown_cell(
            "Les courbes utilisent une valeur marquée, pas un règlement. Pour mesurer un PnL "
            "réalisé, il faut joindre un résultat officiel et respecter la date de résolution."
        ),
    ]
    for index, cell in enumerate(notebook.cells):
        cell["id"] = f"03-{index:02d}"
    output = Path(__file__).resolve().parents[1] / "examples" / "03-toy-backtest.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
