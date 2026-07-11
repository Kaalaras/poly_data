# Polymarket V2 Pipeline

The canonical V2 ingestion path is a narrow Polygon RPC event downloader. It
fetches only the Polymarket V2 exchange logs needed for this repo and writes the
same JSONL row shape as the Ponder indexer, so the existing `order_filled_v2`
Parquet contract remains the raw source of truth.

V1 support is limited to reading existing local `orderFilled` Parquet.

Contracts and events:

- CTF Exchange V2: `0xE111180000d2663C0091e4f400237545B87B996B`
- Neg Risk CTF Exchange V2: `0xe2222d279d744050d28e00520010520000310F59`
- Fill event:
  `OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)`
- Taker marker event:
  `OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)`

Free public RPCs can be slow or pruned, so benchmark the exact V2 log query
before a long run:

```powershell
uv run poly-data benchmark-polygon-rpc
```

Download the V2 logs into a resumable JSONL file:

```powershell
$env:POLYGON_RPC_URL = "https://polygon.drpc.org"
uv run poly-data download-v2-logs `
  --from-block 86126998 `
  --chunk-size 1000
```

The downloader stores progress in `data/_polygon_rpc/cursor.json` and only
advances it after both event types for a range have been fetched, decoded,
written, and flushed. By default it leaves the latest 128 blocks unprocessed
and re-reads the previous 128 blocks on resume; duplicate JSONL rows are safe
because import deduplicates by the raw V2 log ID (`transaction_hash:log_index`).
If a public RPC rejects a range, the downloader shrinks the chunk size and
retries.

Import and process:

```powershell
uv run poly-data import-ponder-v2 data/_polygon_rpc/order_filled_v2_86126998_<end>.jsonl
uv run poly-data compact --source order_filled_v2
uv run poly-data process --source v2
uv run poly-data compact --source trades
uv run poly-data v2-status
```

For the continuous canonical flow, use:

```powershell
uv run poly-data update-all
```

`update-all` downloads direct V2 logs before importing, discovering market
metadata, and deriving trades. It does not ingest Ponder JSONL.

Data model:

- `order_filled_v2`: raw V2 event rows, including the exchange address, log
  index, and V2 metadata for audit; the canonical row ID is
  `transaction_hash:log_index`.
- `market_refreshes`: append-only snapshots of changed Gamma metadata; readers
  resolve the most recently observed value for each market ID.
- `trades`: normalized V1-like maker fills only. V2 taker aggregate rows are not
  flipped into synthetic maker/taker trades.

`poly-data process --source v2` resolves missing market metadata from the V2
`asset` column before joining. If eligible maker-role fills still have
unresolved market metadata after discovery, processing fails without advancing
`data/trades_v2/cursor.json`.

Ponder remains available as an open-source validation/reference indexer in
`indexers/ponder-polymarket-v2`. Use it for bounded comparisons, but do not
import its mutable maker/taker updates into the canonical pipeline.

See `docs/ponder_v2_validation.md` for the bounded pre-cutover and cutover
validation runs.
