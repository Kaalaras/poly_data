import type { Address } from "viem";

export const POLYMARKET_V2_CTF_EXCHANGE =
  "0xE111180000d2663C0091e4f400237545B87B996B" as Address;
export const POLYMARKET_NEG_RISK_CTF_EXCHANGE =
  "0xe2222d279d744050d28e00520010520000310F59" as Address;

// First Polygon block whose timestamp is >= 2026-04-28T11:00:40Z.
export const POLYMARKET_START_BLOCK = Number(process.env.PONDER_START_BLOCK ?? 86126998);

export const USDC_DECIMALS = 1_000_000;
