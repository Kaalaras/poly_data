import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

export type PolyDataOrderFilledV2 = {
  id: string;
  exchange: string;
  log_index: number;
  block_number: number;
  block_timestamp: number;
  transaction_hash: string;
  user_id: string;
  asset: string;
  amount_usdc: number;
  amount_shares: number;
  price: number;
  side: "BUY" | "SELL";
  order_hash: string;
  counterparty_id: string;
  order_type: "maker" | "taker";
  fee: number;
  builder: string;
  metadata: string;
};

export function appendJsonl(row: PolyDataOrderFilledV2): void {
  const outputPath = process.env.POLYMARKET_V2_JSONL_PATH;
  if (outputPath === undefined || outputPath.length === 0) return;
  const resolved = resolve(outputPath);
  mkdirSync(dirname(resolved), { recursive: true });
  appendFileSync(resolved, `${JSON.stringify(row)}\n`, { encoding: "utf8" });
}
