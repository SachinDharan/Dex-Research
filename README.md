# DexResearch

Two projects with Prof. Seoyoung Kim (Santa Clara University) on delegated
authority over on-chain assets.

- [`approval-study/`](./approval-study/) — the ERC-20 `approve()` study:
  four protocol arms (Uniswap, SushiSwap, Compound, Aave) on Ethereum
  mainnet, Nov 2025 – Feb 2026, with the fetch code, pinned SQL, write-ups,
  and the full analysis datasets. Start with its
  [README](./approval-study/README.md). Run the Python modules from inside
  that folder (`cd approval-study && python -m dexresearch.process...`).
- [`joim/`](./joim/) — material for *Beyond Self-Custody: Delegated
  Authority in Tokenized Securities* (Journal of Investment Management,
  Case Studies). [`joim/arcoin/`](./joim/arcoin/) reproduces the ArCoin
  on-chain checks cited in the paper.
