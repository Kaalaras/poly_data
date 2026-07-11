"""Generate examples/06-copy-betting.ipynb."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    nb = nbf.v4.new_notebook()
    md = nbf.v4.new_markdown_cell
    code = nbf.v4.new_code_cell

    cells: list = []

    cells.append(md(
        "# Copy-betting on Polymarket\n\n"
        "Polymarket is technically an orderbook (`maker` posts limit, `taker` "
        "crosses the spread), but for *intent* analytics it's clearer to model "
        "as a sportsbook:\n"
        "- **Punter** = `taker` — they pay the spread to enter a position now.\n"
        "- **Bookmaker** = `maker` — they post passive liquidity.\n\n"
        "Goal of this notebook: reframe trades as bets, define the unit of a "
        "**new bet** (entry event), pick a leader cohort, and backtest a "
        "naive copy-betting strategy on a held-out window. Then size the gap "
        "between leader-actual and copy-bet realised PnL."
    ))

    setup = (
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "import os\n\n"
        "import polars as pl\n"
        "import matplotlib.pyplot as plt\n"
        "import seaborn as sns\n"
        "sns.set_theme(style='whitegrid', context='notebook')\n"
        "plt.rcParams['figure.dpi'] = 110\n\n"
        "from poly_data.analysis.io import scan_trades, scan_markets\n"
        "from poly_data.analysis.punter import (\n"
        "    PLATFORM_WALLETS,\n"
        "    punter_view,\n"
        "    punter_position_timeline,\n"
        "    entries_only,\n"
        "    punter_player_stats,\n"
        "    simulate_copy_bet,\n"
        ")\n"
        "from poly_data.analysis.positions import market_resolution\n"
        "from poly_data.analysis.ranking import select_top_n, score_C\n\n"
        "DATA_ROOT = Path(os.environ.get('POLY_DATA_ROOT', '../data'))\n"
        "assert (DATA_ROOT / 'trades').is_dir(), 'run update-all + process first'\n"
        "trades_lf = scan_trades(DATA_ROOT)\n"
    )
    cells.append(code(setup))

    cells.append(md(
        "## 1. Reframe trades as bets\n\n"
        "Drop platform routers, dust, and extreme prices (settlement-day "
        "cleanup, not real bets). Drop self-trades. Everything below uses "
        "the *taker* side as the punter."
    ))
    cells.append(code(
        "raw_count = trades_lf.select(pl.len()).collect().item()\n"
        "punter_lf = punter_view(trades_lf, min_usd=1.0, price_floor=0.02, price_ceiling=0.98)\n"
        "punter_count = punter_lf.select(pl.len()).collect().item()\n"
        "print(f'raw fills:    {raw_count:>14,}')\n"
        "print(f'punter fills: {punter_count:>14,}  ({punter_count/raw_count:.1%})')\n"
        "print(f'dropped:      {raw_count-punter_count:>14,}')\n"
        "# Bring punter trades into memory for downstream per-row work.\n"
        "punter_df = punter_lf.collect()\n"
        "print('punter_df shape:', punter_df.shape)\n"
    ))

    cells.append(md(
        "## 2. Per-punter position timeline\n\n"
        "Walk fills in chronological order per `(taker, market_id, token_side)`. "
        "Classify each fill: `entry` (prev_position == 0), `add`, `reduce`, "
        "`exit`, `flip`. Copy-betting will key off `entry` events only."
    ))
    cells.append(code(
        "timeline = punter_position_timeline(punter_df)\n"
        "kind_counts = (timeline.group_by('event_kind').len().sort('len', descending=True))\n"
        "print('event distribution:')\n"
        "print(kind_counts)\n"
        "entries = entries_only(timeline)\n"
        "print(f'\\n{entries.height:,} entry events')\n"
    ))

    cells.append(md(
        "## 3. Pick the leader cohort\n\n"
        "Rank punters by `score_C = win_rate * log(max(1, total_won_usd))` over "
        "the **training window** (everything before `train_end_ts`). Filter to "
        "≥20 decided bets and >50% win rate; take top 20. Computed on "
        "taker-only stats so passive market-makers don't dilute the cohort."
    ))
    cells.append(code(
        "max_ts = int(punter_df['timestamp'].max())\n"
        "min_ts = int(punter_df['timestamp'].min())\n"
        "span_days = (max_ts - min_ts) / 86400\n"
        "# Held-out test = last 30 days; training = the rest.\n"
        "test_window_days = 30 if span_days > 60 else max(1, int(span_days * 0.2))\n"
        "train_end_ts = max_ts - test_window_days * 86400\n"
        "print(f'data spans {span_days:.0f} days')\n"
        "print(f'train: ... → {train_end_ts}')\n"
        "print(f'test:  {train_end_ts} → {max_ts}  ({test_window_days}d)')\n\n"
        "train_punter_lf = punter_lf.filter(pl.col('timestamp') < train_end_ts)\n"
        "stats = punter_player_stats(train_punter_lf)\n"
        "leaders_df = select_top_n(stats, n=20, min_win_rate=0.5, min_n_bets=20,\n"
        "                          score_fn=score_C)\n"
        "leaders = set(leaders_df['player'].to_list())\n"
        "print(f'\\n{len(leaders)} leaders selected')\n"
        "leaders_df.select(['player','n_won','n_lost','win_rate','total_won_usd','score']).head(10)\n"
    ))

    cells.append(md(
        "## 4. Backtest copy-betting\n\n"
        "For each leader's `entry` event in the test window, simulate placing "
        "the same direction at the **next punter trade's price** in that "
        "(market, token_side) within a 1-hour window (worst-case slippage "
        "proxy for not having tick-by-tick fills). Hold to resolution; PnL "
        "uses `market_resolution()` over the full trades history.\n\n"
        "Sizing: 2% of a $10,000 bankroll per copy bet (constant — no Kelly "
        "rebalancing; this is a baseline)."
    ))
    cells.append(code(
        "# Resolutions over full trade history (training + test).\n"
        "resolutions = market_resolution(trades_lf)\n"
        "print(f'resolved markets: {(resolutions[\"winner_token\"] != \"open\").sum()}')\n"
        "print(f'still open:       {(resolutions[\"winner_token\"] == \"open\").sum()}')\n\n"
        "result = simulate_copy_bet(\n"
        "    entries=entries,\n"
        "    all_punter_trades=punter_df,\n"
        "    resolutions=resolutions,\n"
        "    leaders=leaders,\n"
        "    train_end_ts=train_end_ts,\n"
        "    test_end_ts=max_ts,\n"
        "    bankroll=10_000.0,\n"
        "    per_bet_frac=0.02,\n"
        "    slippage_window_secs=3600,\n"
        ")\n"
        "print('summary:', result.summary)\n"
        "result.bets.head(10)\n"
    ))

    cells.append(md(
        "## 5. Equity curve & realised PnL distribution\n\n"
        "Sort copy bets by `fill_ts`, take cumulative PnL of resolved bets. "
        "Stack against a naive baseline: a random-leader cohort of the same "
        "size."
    ))
    cells.append(code(
        "if result.bets.height > 0:\n"
        "    resolved = result.bets.filter(pl.col('resolved')).sort('fill_ts')\n"
        "    eq = resolved.with_columns(pl.col('pnl_usd').cum_sum().alias('cum_pnl'))\n"
        "    fig, axes = plt.subplots(1, 2, figsize=(13, 4))\n"
        "    axes[0].plot(eq['cum_pnl'].to_numpy())\n"
        "    axes[0].axhline(0, color='k', lw=0.5)\n"
        "    axes[0].set_title(f'Copy-bet equity curve ({eq.height} resolved bets)')\n"
        "    axes[0].set_xlabel('bet #'); axes[0].set_ylabel('cumulative PnL (USD)')\n\n"
        "    axes[1].hist(resolved['pnl_usd'].to_numpy(), bins=40, color='#2563eb')\n"
        "    axes[1].axvline(0, color='k', lw=0.5)\n"
        "    axes[1].set_title('Per-bet PnL distribution')\n"
        "    axes[1].set_xlabel('PnL (USD)'); axes[1].set_ylabel('count')\n"
        "    plt.tight_layout(); plt.show()\n"
        "else:\n"
        "    print('No copy bets — leader cohort had no entries in test window.')\n"
    ))

    cells.append(md(
        "## 6. Slippage analysis\n\n"
        "Distribution of `fill_price − leader_price`. A positive value on a "
        "BUY means the copier paid more (worse fill), eroding edge. Real "
        "production deployment would need this to be near-zero (low-latency "
        "websocket / onchain monitoring)."
    ))
    cells.append(code(
        "if result.bets.height > 0:\n"
        "    fig, ax = plt.subplots(figsize=(9, 3.5))\n"
        "    sl = result.bets['slippage'].drop_nulls().to_numpy()\n"
        "    ax.hist(sl, bins=50, color='#7c3aed')\n"
        "    ax.axvline(0, color='k', lw=0.5)\n"
        "    ax.set_title(f'Slippage distribution (median={float(pl.Series(sl).median()):+.4f})')\n"
        "    ax.set_xlabel('fill_price − leader_price')\n"
        "    plt.tight_layout(); plt.show()\n"
    ))

    cells.append(md(
        "## 7. Caveats for production\n\n"
        "- **Edge erosion**: If many copiers, each leader trade gets front-run "
        "  by copybots — leader's edge collapses. The historical backtest "
        "  *cannot* model this; assume realised live PnL ≪ backtested PnL.\n"
        "- **Survivorship bias**: top-N selection on past performance has a "
        "  large look-ahead component baked in. Cross-validate by sliding the "
        "  train/test split forward.\n"
        "- **Latency**: this notebook proxies slippage with the *next punter "
        "  trade's price*. Live, you need onchain monitoring (mempool / "
        "  Polymarket websocket); assume fill latency in seconds, not hours.\n"
        "- **Sizing**: 2% flat is a placeholder. Kelly half-fraction per "
        "  leader's per-bet edge is a saner production policy.\n"
        "- **Position vs trade copying**: this notebook copies *entry events* "
        "  only. Adds, exits, and flips are leader-specific path decisions; "
        "  generic copying creates path-dependent portfolios that diverge "
        "  from leader after a few weeks."
    ))

    nb.cells = cells
    out = Path(__file__).resolve().parents[1] / "examples" / "06-copy-betting.ipynb"
    out.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(nb, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
