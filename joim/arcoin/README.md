# ArCoin authority checks

Reproduces the two on-chain tests cited in *Beyond Self-Custody: Delegated
Authority in Tokenized Securities* (Seoyoung Kim and Sachin Muralidharan,
Journal of Investment Management, Case Studies). Citation to be added on
publication.

ArCoin (RCOIN) is the tokenized share of the Arca U.S. Treasury Fund. Its
active deployment is an upgradeable proxy at
[`0x252739487c1fa66eaeae7ced41d6358ab2a6bca9`](https://etherscan.io/address/0x252739487c1fa66eaeae7ced41d6358ab2a6bca9#code)
whose current implementation contract is unverified. The paper reports that,
at block 26029684:

1. a `transfer` of one unit from the largest holder to another existing holder
   reverts with the contract's own message `Tokens Locked`;
2. an `approve` call from an arbitrary address that has never held RCOIN
   succeeds.

Both are `eth_call` simulations: the node runs the deployed code against chain
state without signing or broadcasting anything. Nothing moves, no private key
is needed, and the `from` address can be any account.

## Run it

Python (standard library only, no packages):

```sh
python3 check_arcoin.py
python3 check_arcoin.py --rpc https://eth.drpc.org   # any archive endpoint
python3 check_arcoin.py --block latest               # current state instead of the pinned block
```

Plain curl (the sequence printed in the paper's appendix):

```sh
sh check_arcoin.sh
RPC=https://eth.drpc.org sh check_arcoin.sh
```

Docker:

```sh
docker build -t arcoin-check . && docker run --rm arcoin-check
```

Expected output ends with `RESULT: matches the paper`. The script exits
non-zero if either result differs, which would mean the contract state or
implementation changed after block 26029684.

## Notes

- The default endpoint is [Tenderly](https://tenderly.co)'s public gateway,
  `https://mainnet.gateway.tenderly.co`. Tenderly is an Ethereum
  infrastructure company that runs full-history ("archive") nodes and lets
  anyone query them over HTTPS with no account or API key. The answers come
  from the deployed contract code on Ethereum, not from Tenderly; the same
  checks give the same results through any other archive provider (for
  example `https://eth.drpc.org`, also keyless). A non-archive node answers
  only `--block latest`.
- The holder addresses are the two largest balances at block 26029684,
  computed from the token's 400 `Transfer` events (25 holders in total) and
  confirmed with `balanceOf`.
- The `from` address for `approve` is `0x…dEaD`, chosen because it has never
  held RCOIN. Any address gives the same result.
