"""Generate the direction-aware V2 copy-betting notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 06 — Expérience historique de copie directionnelle\n\n"
            "> Ceci est une expérience historique, pas une stratégie live. Les entrées sont "
            "exécutées avec latence, réservées en capital et réglées à la date officielle."
        ),
        nbf.v4.new_code_cell(
            "import polars as pl\n\n"
            "from poly_data.analysis.punter import entries_only, punter_position_timeline, punter_view, simulate_copy_bet, simulate_random_cohort\n"
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context(); store = ParquetStore(ctx.root)\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision})\n"
            "source_inventory(store, ['trades', 'market_outcomes'])"
        ),
        nbf.v4.new_code_cell(
            "punters = punter_view(store.scan('trades'), price_floor=0.02, price_ceiling=0.98).collect()\n"
            "entries = entries_only(punter_position_timeline(punters))\n"
            "outcomes = store.scan('market_outcomes').select(['market_id', 'winner_token', 'resolved_at']).collect()\n"
            "cutoff = int(entries['timestamp'].quantile(0.7))\n"
            "leaders = set(entries.filter(pl.col('timestamp') < cutoff).group_by('taker').len().sort('len', descending=True).head(5)['taker'].to_list())\n"
            "print({'entries': entries.height, 'leaders': len(leaders), 'cutoff': cutoff})"
        ),
        nbf.v4.new_code_cell(
            "result = simulate_copy_bet(entries, punters, outcomes, leaders, train_end_ts=cutoff, test_end_ts=int(entries['timestamp'].max()) + 1, latency_secs=60, fee_bps=10, random_seed=7)\n"
            "result.bets"
        ),
        nbf.v4.new_code_cell(
            "control = simulate_random_cohort(entries.filter(pl.col('timestamp') < cutoff), seed=7, n_leaders=len(leaders))\n"
            "print({'random_leaders': sorted(control.leaders), **result.summary})\n"
            "result.cashflows.sort('timestamp')"
        ),
        nbf.v4.new_markdown_cell(
            "Les flux négatifs arrivent à l'exécution ; les flux positifs n'arrivent qu'au "
            "règlement officiel. Les marchés non réglés restent du capital réservé, et les "
            "comparaisons de cohortes aléatoires doivent être répétées sur plusieurs seeds."
        ),
    ]
    for index, cell in enumerate(notebook.cells):
        cell["id"] = f"06-{index:02d}"
    output = Path(__file__).resolve().parents[1] / "examples" / "06-copy-betting.ipynb"
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
