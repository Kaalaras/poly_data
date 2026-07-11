import { onchainTable } from "ponder";

export const orderFilledV2 = onchainTable("order_filled_v2", (t) => ({
  id: t.text().primaryKey(),
  exchange: t.text().notNull(),
  blockNumber: t.bigint().notNull(),
  blockTimestamp: t.bigint().notNull(),
  transactionHash: t.text().notNull(),
  logIndex: t.integer().notNull(),
  orderHash: t.text().notNull(),
  userId: t.text().notNull(),
  asset: t.text().notNull(),
  amountUsdc: t.doublePrecision().notNull(),
  amountShares: t.doublePrecision().notNull(),
  price: t.doublePrecision().notNull(),
  side: t.text().notNull(),
  orderType: t.text().notNull(),
  counterpartyId: t.text().notNull(),
  fee: t.doublePrecision().notNull(),
  builder: t.text().notNull(),
  metadata: t.text().notNull(),
}));
