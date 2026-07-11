# Ponder V2 Validation

Validation runs on 2026-05-27 used Polygon dRPC with
`PONDER_ETH_GET_LOGS_BLOCK_RANGE=1000`.

## Windows Checked

| Window | Blocks | RPC `OrderFilled` logs | Ponder JSONL lines | Unique V2 fills | Initial normalized trades |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pre-cutover preview | `85977578..85978578` | 6 | 9 | 6 | 0 |
| Cutover smoke | `86126998..86127998` | 31 | 46 | 31 | 16 |

The JSONL line count can exceed unique fills because `OrdersMatched` updates the
same fill from `maker` to `taker`; the importer keeps the last row per `id`.

## Findings

- The Ponder ABI/address pair matches raw Polygon logs in both sampled windows.
- The April 25 preview rows decode correctly, but their token IDs are not
  returned by the public Gamma markets API and are absent from local
  `markets`/`missing_markets`, so they cannot be normalized into `trades`.
- The cutover window joins successfully against existing market metadata: 16
  normalized trades, 4 markets, prices in `[0.001, 0.999]`, and positive
  amounts.
- There is no exact V1 overlap in the April 25 preview window in the local
  processed `trades` table, so validation is based on raw RPC parity plus V2
  contract checks rather than V1 transaction-hash equality.

## Decision

Use direct Polygon RPC logs as the only active V2 ingest. Keep Ponder for
bounded, independent parity checks and keep existing V1 Parquet read-only for
legacy local processing.
