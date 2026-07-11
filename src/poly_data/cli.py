from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from poly_data.benchmark import benchmark_source
from poly_data.compact.monthly import compact_all
from poly_data.distribute.huggingface import push_snapshot
from poly_data.ingest.discover import discover_and_fetch
from poly_data.ingest.markets import update_markets
from poly_data.ingest.ponder import import_ponder_v2_jsonl
from poly_data.ingest.polygon_rpc import (
    benchmark_polygon_rpc,
    default_rpc_url,
    download_v2_logs,
)
from poly_data.ingest.v2_status import build_v2_status
from poly_data.io.parquet_store import ParquetStore
from poly_data.logging_setup import configure_logging
from poly_data.process.trades import process_trades
from poly_data.quality import validate_store

logger = logging.getLogger(__name__)


def _discover_and_fetch_missing_tokens(
    store: ParquetStore,
    *,
    source: str = "all",
    batch_size: int = 100,
    workers: int = 8,
    rate_per_sec: float = 8.0,
) -> int:
    """Lazy polars anti-join + batched parallel fetch with negative cache.

    Replaces the previous Python-set materialisation + serial-per-token
    approach (~27 h on 195k missing IDs) with a streaming anti-join (seconds)
    plus batch-CSV gamma calls run concurrently — typically minutes, with
    negative caching so re-runs don't re-fetch known-dead IDs.
    """
    sources = {
        "v1": ["orderFilled"],
        "v2": ["order_filled_v2"],
        "all": ["orderFilled", "order_filled_v2"],
    }[source]
    return discover_and_fetch(
        store, sources=sources, batch_size=batch_size, workers=workers,
        rate_per_sec=rate_per_sec,
    )


def _default_ponder_jsonl(store: ParquetStore) -> Path:
    configured = os.environ.get("POLYMARKET_V2_JSONL_PATH")
    if configured:
        return Path(configured)
    return store.root / "_ponder" / "order_filled_v2.jsonl"


def _build_parser() -> argparse.ArgumentParser:
    # Shared options inherited by every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-root", default="data",
                        help="root dir for the Parquet store (default: ./data)")
    common.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    p = argparse.ArgumentParser(prog="poly-data", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("update-markets", parents=[common],
                   help="fetch Polymarket markets")

    pr = sub.add_parser("process", parents=[common],
                        help="derive trades from local raw fill sources")
    pr.add_argument("--source", choices=["v1", "v2", "all"], default="all",
                    help="raw source to process (default: all)")
    ip = sub.add_parser("import-ponder-v2", parents=[common],
                        help="import Ponder Polymarket V2 JSONL into Parquet")
    ip.add_argument("jsonl", nargs="?", default=None,
                    help="JSONL path (default: POLYMARKET_V2_JSONL_PATH or data/_ponder/order_filled_v2.jsonl)")
    ip.add_argument("--batch-size", type=int, default=100_000)
    dl = sub.add_parser("download-v2-logs", parents=[common],
                        help="download Polymarket V2 exchange logs from Polygon RPC")
    dl.add_argument("--rpc-url", default=None,
                    help="Polygon RPC URL (default: POLYGON_RPC_URL, PONDER_RPC_URL_137, or dRPC)")
    dl.add_argument("--from-block", type=int, default=None,
                    help="start block (default: saved cursor or V2 cutover)")
    dl.add_argument("--to-block", type=int, default=None,
                    help="end block inclusive (default: latest block)")
    dl.add_argument("--output", type=Path, default=None,
                    help="JSONL output path (default: data/_polygon_rpc/order_filled_v2_<range>.jsonl)")
    dl.add_argument("--cursor", type=Path, default=None,
                    help="cursor path (default: data/_polygon_rpc/cursor.json)")
    dl.add_argument("--chunk-size", type=int, default=1_000)
    dl.add_argument("--min-chunk-size", type=int, default=25)
    dl.add_argument("--max-retries", type=int, default=5)
    dl.add_argument("--timeout", type=float, default=30.0)
    dl.add_argument("--sleep-seconds", type=float, default=0.0)
    dl.add_argument(
        "--confirmations",
        type=int,
        default=128,
        help="leave this many head blocks unprocessed for finality (default: 128)",
    )
    dl.add_argument(
        "--overlap-blocks",
        type=int,
        default=128,
        help="re-fetch this many completed blocks on resume (default: 128)",
    )
    dl.add_argument("--limit-ranges", type=int, default=None,
                    help="stop after N completed ranges for smoke tests")
    bench = sub.add_parser("benchmark-polygon-rpc", parents=[common],
                           help="benchmark Polygon RPC endpoints for V2 event logs")
    bench.add_argument("--rpc-url", action="append", default=None,
                       help="endpoint to benchmark; repeat for multiple endpoints")
    bench.add_argument("--from-block", type=int, default=86_127_999)
    bench.add_argument("--span", action="append", type=int, default=None,
                       help="block span to test; repeat to override defaults")
    bench.add_argument("--timeout", type=float, default=15.0)
    lake_bench = sub.add_parser("benchmark-lake", parents=[common],
                                help="benchmark a local Parquet source")
    lake_bench.add_argument("--source", required=True, help="source to scan")

    c = sub.add_parser("compact", parents=[common],
                       help="compact month partitions")
    c.add_argument("--source", default=None)

    h = sub.add_parser("push-hf", parents=[common],
                        help="push snapshot to HuggingFace Hub")
    h.add_argument("--repo", required=True)
    h.add_argument("--source", action="append", default=None)

    d = sub.add_parser("discover-missing", parents=[common],
                       help="fetch markets for token IDs referenced by "
                            "local raw fills but absent from markets")
    d.add_argument("--batch-size", type=int, default=25,
                   help="ids per gamma request (array-repeat). Auto-shrinks "
                        "on HTTP 414. Capped to ~30 by URI length budget.")
    d.add_argument("--workers", type=int, default=8)
    d.add_argument("--rate-per-sec", type=float, default=8.0,
                   help="global gamma req/s ceiling (token bucket)")
    d.add_argument("--max-ids", type=int, default=None,
                   help="cap on number of ids to fetch this run")

    sub.add_parser(
        "update-all",
        parents=[common],
        help="markets + canonical V2 RPC logs + missing markets + process",
    )
    sub.add_parser("v2-status", parents=[common],
                   help="summarize V2 raw, derived trades, and public API freshness")
    val = sub.add_parser("validate", parents=[common],
                         help="validate local Parquet lake quality")
    val.add_argument("--source", action="append", default=None,
                     help="source to validate; repeat for multiple sources")
    val.add_argument("--full", action="store_true",
                     help="run duplicate and referential integrity checks")
    val.add_argument("--strict", action="store_true",
                     help="return non-zero on warnings as well as errors")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    configure_logging(level=getattr(logging, ns.log_level))

    store = ParquetStore(Path(ns.data_root))

    if ns.cmd == "update-markets":
        n = update_markets(store)
        logger.info("update-markets: %d new rows", n)
        return 0

    if ns.cmd == "discover-missing":
        n = discover_and_fetch(
            store,
            sources=["orderFilled", "order_filled_v2"],
            max_ids=ns.max_ids,
            batch_size=ns.batch_size,
            workers=ns.workers,
            rate_per_sec=ns.rate_per_sec,
        )
        logger.info("discover-missing: %d new markets", n)
        return 0

    if ns.cmd == "import-ponder-v2":
        path = Path(ns.jsonl) if ns.jsonl else _default_ponder_jsonl(store)
        n = import_ponder_v2_jsonl(
            path,
            store=store,
            batch_size=ns.batch_size,
        )
        logger.info("import-ponder-v2: %d raw V2 rows", n)
        return 0
    if ns.cmd == "download-v2-logs":
        summary = download_v2_logs(
            data_root=store.root,
            rpc_url=ns.rpc_url,
            from_block=ns.from_block,
            to_block=ns.to_block,
            output_path=ns.output,
            cursor_path=ns.cursor,
            chunk_size=ns.chunk_size,
            min_chunk_size=ns.min_chunk_size,
            max_retries=ns.max_retries,
            timeout=ns.timeout,
            sleep_seconds=ns.sleep_seconds,
            confirmations=ns.confirmations,
            overlap_blocks=ns.overlap_blocks,
            limit_ranges=ns.limit_ranges,
        )
        logger.info(
            "download-v2-logs: wrote %d rows from %d ranges to %s",
            summary.rows,
            summary.ranges,
            summary.output_path,
        )
        return 0
    if ns.cmd == "benchmark-polygon-rpc":
        endpoints = ns.rpc_url or [default_rpc_url()]
        spans = tuple(ns.span) if ns.span else (50, 250, 500, 1_000, 1_500)
        print(json.dumps(
            benchmark_polygon_rpc(
                rpc_urls=endpoints,
                from_block=ns.from_block,
                spans=spans,
                timeout=ns.timeout,
            ),
            indent=2,
            sort_keys=True,
        ))
        return 0

    if ns.cmd == "benchmark-lake":
        print(json.dumps(benchmark_source(store, ns.source), indent=2, sort_keys=True))
        return 0

    if ns.cmd == "process":
        # Discover-and-fetch missing tokens BEFORE deriving trades so the join
        # against `markets`/`missing_markets` resolves token IDs that ingest
        # never observed (else trades land with null market_id).
        n_missing = _discover_and_fetch_missing_tokens(store, source=ns.source)
        if n_missing:
            logger.info("process: fetched %d missing markets", n_missing)
        n = process_trades(store, source=ns.source)
        logger.info("process: %d new trades", n)
        return 0

    if ns.cmd == "compact":
        sources = [ns.source] if ns.source else \
            [
                "orderFilled", "order_filled_v2", "markets", "missing_markets",
                "market_refreshes", "trades",
            ]
        for s in sources:
            compact_all(store, s)
        return 0

    if ns.cmd == "push-hf":
        url = push_snapshot(store, repo_id=ns.repo, sources=ns.source)
        logger.info("pushed: %s", url)
        return 0

    if ns.cmd == "v2-status":
        print(json.dumps(build_v2_status(store), indent=2, sort_keys=True))
        return 0

    if ns.cmd == "validate":
        report = validate_store(store, sources=ns.source, full=ns.full)
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["status"] == "error":
            return 1
        if ns.strict and report["status"] == "warning":
            return 1
        return 0

    if ns.cmd == "update-all":
        update_markets(store)
        summary = download_v2_logs(data_root=store.root)
        n_raw_v2 = import_ponder_v2_jsonl(summary.output_path, store=store)
        logger.info(
            "update-all: downloaded %d V2 rows from %d RPC ranges and imported %d rows",
            summary.rows,
            summary.ranges,
            n_raw_v2,
        )
        n_missing = _discover_and_fetch_missing_tokens(store, source="all")
        if n_missing:
            logger.info("update-all: fetched %d missing markets", n_missing)
        process_trades(store, source="all")
        return 0

    parser.error(f"unknown command: {ns.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
