# Ponder Polymarket V2 Indexer

This is the optional open-source Polymarket V2 fill indexer for `poly-data`.
It is useful for validation/reference runs when a strong archive RPC is
available. The default free backfill path is `poly-data download-v2-logs`, which
downloads the same event shape without running a full Ponder index.

## Source Of Truth

- Polymarket V2 CTF Exchange: `0xE111180000d2663C0091e4f400237545B87B996B`
- Polymarket Neg Risk CTF Exchange: `0xe2222d279d744050d28e00520010520000310F59`
- Event:
  `OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)`
- Taker marker event:
  `OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)`

The default start block is `86126998`, the first Polygon block at or after
`2026-04-28T11:00:40Z`, the Polymarket V2 cutover used by this repo.

## Run

```powershell
cd indexers/ponder-polymarket-v2
$env:NODE_OPTIONS = "--use-system-ca"
$env:DATABASE_SCHEMA = "poly_data_ponder_v2"
npm install
Copy-Item .env.example .env.local
npm run codegen
npm run start
```

Set `PONDER_RPC_URL_137` to a reliable Polygon archive RPC before a serious
backfill. Public near-head RPCs are useful for a smoke test, but many prune old
log history and fail the April/May 2026 backfill. Keep
`PONDER_ETH_GET_LOGS_BLOCK_RANGE` small for free RPCs; `1000` is a conservative
default.

Rows are also appended to `POLYMARKET_V2_JSONL_PATH` if that env var is set.
Import them into the Python Parquet lake with:

```powershell
uv run poly-data import-ponder-v2 data/_ponder/order_filled_v2.jsonl
uv run poly-data process --source v2
```

`order_filled_v2` keeps both maker fills and taker aggregate rows for audit.
The shared `trades` table is maker-fill-only to stay close to the V1 economic
fill model.
