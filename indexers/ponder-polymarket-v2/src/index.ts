import { ponder } from "ponder:registry";
import schema from "ponder:schema";
import type { Hex } from "viem";

import { USDC_DECIMALS } from "./constants";
import { appendJsonl, type PolyDataOrderFilledV2 } from "./jsonl";

type Side = "BUY" | "SELL";
type OrderType = "maker" | "taker";

function txHash(event: any): string {
  const value = event.transaction?.hash ?? event.log.transactionHash;
  if (value === undefined) throw new Error("event is missing transaction hash");
  return value;
}

function eventId(transactionHash: string, orderHash: Hex): string {
  return `${transactionHash}:${orderHash}`;
}

function sideName(side: number | bigint): Side {
  return Number(side) === 0 ? "BUY" : "SELL";
}

function decimal(raw: bigint): number {
  return Number(raw) / USDC_DECIMALS;
}

function splitAmounts(
  side: Side,
  makerAmountFilled: bigint,
  takerAmountFilled: bigint,
): { amountUsdc: number; amountShares: number; price: number } {
  const makerAmount = decimal(makerAmountFilled);
  const takerAmount = decimal(takerAmountFilled);
  const amountUsdc = side === "BUY" ? makerAmount : takerAmount;
  const amountShares = side === "BUY" ? takerAmount : makerAmount;
  return {
    amountUsdc,
    amountShares,
    price: amountShares > 0 ? amountUsdc / amountShares : 0,
  };
}

function toJsonl(row: {
  id: string;
  blockNumber: bigint;
  blockTimestamp: bigint;
  transactionHash: string;
  exchange: string;
  logIndex: number;
  orderHash: string;
  userId: string;
  asset: string;
  amountUsdc: number;
  amountShares: number;
  price: number;
  side: string;
  orderType: string;
  counterpartyId: string;
  fee: number;
  builder: string;
  metadata: string;
}): PolyDataOrderFilledV2 {
  return {
    id: row.id,
    exchange: row.exchange,
    log_index: row.logIndex,
    block_number: Number(row.blockNumber),
    block_timestamp: Number(row.blockTimestamp),
    transaction_hash: row.transactionHash,
    user_id: row.userId,
    asset: row.asset,
    amount_usdc: row.amountUsdc,
    amount_shares: row.amountShares,
    price: row.price,
    side: row.side as Side,
    order_hash: row.orderHash,
    counterparty_id: row.counterpartyId,
    order_type: row.orderType as OrderType,
    fee: row.fee,
    builder: row.builder,
    metadata: row.metadata,
  };
}

ponder.on("CtfExchangeV2:OrderFilled", async ({ event, context }) => {
  await upsertOrderFilled(event, context, "CtfExchangeV2");
});

ponder.on("NegRiskCtfExchangeV2:OrderFilled", async ({ event, context }) => {
  await upsertOrderFilled(event, context, "NegRiskCtfExchangeV2");
});

ponder.on("CtfExchangeV2:OrdersMatched", async ({ event, context }) => {
  await markTakerOrder(event, context);
});

ponder.on("NegRiskCtfExchangeV2:OrdersMatched", async ({ event, context }) => {
  await markTakerOrder(event, context);
});

async function upsertOrderFilled(
  event: any,
  context: any,
  exchange: string,
): Promise<void> {
  const {
    orderHash,
    maker,
    taker,
    side,
    tokenId,
    makerAmountFilled,
    takerAmountFilled,
    fee,
    builder,
    metadata,
  } = event.args;
  const transactionHash = txHash(event);
  const orderSide = sideName(side);
  const amounts = splitAmounts(orderSide, makerAmountFilled, takerAmountFilled);
  const id = eventId(transactionHash, orderHash);
  const row = {
    id,
    exchange,
    blockNumber: event.block.number,
    blockTimestamp: event.block.timestamp,
    transactionHash,
    logIndex: Number(event.log.logIndex),
    orderHash,
    userId: maker,
    asset: tokenId.toString(),
    amountUsdc: amounts.amountUsdc,
    amountShares: amounts.amountShares,
    price: amounts.price,
    side: orderSide,
    orderType: "maker" as OrderType,
    counterpartyId: taker,
    fee: decimal(fee),
    builder,
    metadata,
  };
  await context.db
    .insert(schema.orderFilledV2)
    .values(row)
    .onConflictDoUpdate((existing: { orderType: OrderType }) => ({
      ...row,
      orderType: existing.orderType,
    }));
  appendJsonl(toJsonl(row));
}

async function markTakerOrder(
  event: any,
  context: any,
): Promise<void> {
  const transactionHash = txHash(event);
  const id = eventId(transactionHash, event.args.takerOrderHash);
  const existing = await context.db.find(schema.orderFilledV2, { id });
  if (existing === null) return;
  await context.db.update(schema.orderFilledV2, { id }).set({ orderType: "taker" });
  appendJsonl(toJsonl({ ...existing, orderType: "taker" }));
}
