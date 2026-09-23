#!/usr/bin/env python3
"""Reproduce the ArCoin (RCOIN) authority checks cited in
"Beyond Self-Custody: Delegated Authority in Tokenized Securities"
(Kim & Muralidharan, Journal of Investment Management, Case Studies).

Two read-only simulations (eth_call) against the live ArCoin proxy at a
pinned block. eth_call executes the deployed contract code against chain
state without signing or broadcasting anything, so the `from` address can
be any account; nothing is moved and no key is needed.

  1. transfer(1 unit) from the largest holder to another existing holder
     -> reverts with the contract's own message "Tokens Locked"
  2. approve(spender, 1) from an arbitrary address that never held RCOIN
     -> succeeds (returns true)

Needs only Python 3.8+ (standard library) and an archive RPC endpoint.

    python3 check_arcoin.py                       # public Tenderly gateway
    python3 check_arcoin.py --rpc https://eth.drpc.org
    python3 check_arcoin.py --block latest        # current state instead of the pinned block
"""
import argparse
import json
import sys
import urllib.request

PROXY = "0x252739487c1fa66eaeae7ced41d6358ab2a6bca9"   # ArCoin active proxy (verified)
BLOCK = 26029684                                        # state cited in the paper
HOLDER_FROM = "0x0962e5e430c512005e1345af2b05d260e9347099"  # largest holder at BLOCK
HOLDER_TO = "0xb6ffc6f5f6c98dc08348760994fe55522fe61401"    # second-largest holder at BLOCK
STRANGER = "0x000000000000000000000000000000000000dEaD"     # never held RCOIN
SPENDER = "0x1111111111111111111111111111111111111111"
DEFAULT_RPC = "https://mainnet.gateway.tenderly.co"

# ERC-20 function selectors
BALANCE_OF, TRANSFER, APPROVE = "0x70a08231", "0xa9059cbb", "0x095ea7b3"


def word(x) -> str:
    return (x[2:] if isinstance(x, str) else hex(x)[2:]).rjust(64, "0")


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "arcoin-check"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def decode_revert(data: str) -> str:
    # Error(string) = 0x08c379a0 + offset + length + utf8 bytes
    if isinstance(data, str) and data.startswith("0x08c379a0"):
        n = int(data[74:138], 16)
        return bytes.fromhex(data[138:138 + 2 * n]).decode()
    return str(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--block", default=str(BLOCK), help="block number or 'latest'")
    a = ap.parse_args()
    blk = a.block if a.block == "latest" else hex(int(a.block))
    ok = True

    for label, addr in (("largest holder", HOLDER_FROM), ("second holder", HOLDER_TO)):
        r = rpc(a.rpc, "eth_call", [{"to": PROXY, "data": BALANCE_OF + word(addr)}, blk])
        print(f"balanceOf({label} {addr}) = {int(r['result'], 16)}")

    r = rpc(a.rpc, "eth_call", [{"from": HOLDER_FROM, "to": PROXY,
                                 "data": TRANSFER + word(HOLDER_TO) + word(1)}, blk])
    if "error" in r:
        reason = decode_revert(r["error"].get("data", ""))
        print(f"transfer(1) from largest holder -> REVERTED: {reason!r}")
        ok &= reason == "Tokens Locked"
    else:
        print(f"transfer(1) from largest holder -> succeeded ({r['result']}); state has changed since block {BLOCK}")
        ok = False

    r = rpc(a.rpc, "eth_call", [{"from": STRANGER, "to": PROXY,
                                 "data": APPROVE + word(SPENDER) + word(1)}, blk])
    if "error" in r:
        print(f"approve(1) from stranger -> REVERTED: {decode_revert(r['error'].get('data', ''))!r}")
        ok = False
    else:
        success = int(r["result"], 16) == 1
        print(f"approve(1) from stranger -> returned {r['result']} ({'true' if success else 'false'})")
        ok &= success

    print("\nRESULT:", "matches the paper" if ok else "DOES NOT match the paper")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
