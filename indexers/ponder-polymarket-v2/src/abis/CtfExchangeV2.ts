export const ctfExchangeV2Abi = [
  {
    type: "event",
    name: "OrderFilled",
    inputs: [
      { name: "orderHash", type: "bytes32", indexed: true },
      { name: "maker", type: "address", indexed: true },
      { name: "taker", type: "address", indexed: true },
      { name: "side", type: "uint8", indexed: false },
      { name: "tokenId", type: "uint256", indexed: false },
      { name: "makerAmountFilled", type: "uint256", indexed: false },
      { name: "takerAmountFilled", type: "uint256", indexed: false },
      { name: "fee", type: "uint256", indexed: false },
      { name: "builder", type: "bytes32", indexed: false },
      { name: "metadata", type: "bytes32", indexed: false }
    ],
    anonymous: false
  },
  {
    type: "event",
    name: "OrdersMatched",
    inputs: [
      { name: "takerOrderHash", type: "bytes32", indexed: true },
      { name: "takerOrderMaker", type: "address", indexed: true },
      { name: "side", type: "uint8", indexed: false },
      { name: "tokenId", type: "uint256", indexed: false },
      { name: "makerAmountFilled", type: "uint256", indexed: false },
      { name: "takerAmountFilled", type: "uint256", indexed: false }
    ],
    anonymous: false
  }
] as const;
