#!/usr/bin/env sh
# Same two checks as check_arcoin.py, as plain curl commands (Appendix A of the paper).
# Any Ethereum archive RPC works; override with RPC=https://... ./check_arcoin.sh
set -eu
RPC="${RPC:-https://mainnet.gateway.tenderly.co}"
PROXY=0x252739487c1fa66eaeae7ced41d6358ab2a6bca9        # ArCoin active proxy
BLOCK=0x18d2e74                                          # 26029684
HOLDER_FROM=0x0962e5e430c512005e1345af2b05d260e9347099   # largest holder at BLOCK
HOLDER_TO=0xb6ffc6f5f6c98dc08348760994fe55522fe61401     # another existing holder
STRANGER=0x000000000000000000000000000000000000dEaD      # never held RCOIN

call() { curl -s "$RPC" -H 'Content-Type: application/json' -d "$1"; echo; }

echo "1) transfer(HOLDER_TO, 1) simulated from the largest holder -> expect 'Tokens Locked'"
call "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_call\",\"params\":[{\"from\":\"$HOLDER_FROM\",\"to\":\"$PROXY\",
  \"data\":\"0xa9059cbb000000000000000000000000${HOLDER_TO#0x}0000000000000000000000000000000000000000000000000000000000000001\"},\"$BLOCK\"]}"

echo "2) approve(0x1111..., 1) simulated from an address that never held RCOIN -> expect result 0x...01 (true)"
call "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"eth_call\",\"params\":[{\"from\":\"$STRANGER\",\"to\":\"$PROXY\",
  \"data\":\"0x095ea7b300000000000000000000000011111111111111111111111111111111111111110000000000000000000000000000000000000000000000000000000000000001\"},\"$BLOCK\"]}"
