import { createConfig } from "ponder";
import { http } from "viem";

import { ctfExchangeV2Abi } from "./src/abis/CtfExchangeV2";
import {
  POLYMARKET_NEG_RISK_CTF_EXCHANGE,
  POLYMARKET_START_BLOCK,
  POLYMARKET_V2_CTF_EXCHANGE,
} from "./src/constants";

const rpcUrl = process.env.PONDER_RPC_URL_137;
if (rpcUrl === undefined || rpcUrl.length === 0) {
  throw new Error("PONDER_RPC_URL_137 is required");
}

const endBlock =
  process.env.PONDER_END_BLOCK === undefined || process.env.PONDER_END_BLOCK === ""
    ? undefined
    : Number(process.env.PONDER_END_BLOCK);
const ethGetLogsBlockRange = Number(process.env.PONDER_ETH_GET_LOGS_BLOCK_RANGE ?? 1000);

export default createConfig({
  chains: {
    polygon: {
      id: 137,
      rpc: http(rpcUrl),
      ethGetLogsBlockRange,
    },
  },
  contracts: {
    CtfExchangeV2: {
      abi: ctfExchangeV2Abi,
      chain: "polygon",
      address: POLYMARKET_V2_CTF_EXCHANGE,
      startBlock: POLYMARKET_START_BLOCK,
      endBlock,
    },
    NegRiskCtfExchangeV2: {
      abi: ctfExchangeV2Abi,
      chain: "polygon",
      address: POLYMARKET_NEG_RISK_CTF_EXCHANGE,
      startBlock: POLYMARKET_START_BLOCK,
      endBlock,
    },
  },
});
