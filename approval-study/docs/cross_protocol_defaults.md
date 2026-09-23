# Cross-protocol test: the same wallet under two approval defaults

**Question** (Prof. Kim): among wallets that used both Uniswap and SushiSwap,
do those who accept the default unlimited allowance on Uniswap opt in to
unlimited on SushiSwap — where unlimited is *not* the default?

**Answer: the data support the thesis, in a stronger form than posed.**
Wallets that hold an unlimited allowance on the Uniswap path split roughly
in half on Sushi (46.8% granted only exact amounts there). But the reverse
behavior — deliberately choosing exact on Uniswap while choosing unlimited
on Sushi — is essentially nonexistent: **469 wallets go unlimited-on-Uniswap
/ exact-on-Sushi versus 1 wallet the other way.** Under the null that the
protocol makes no difference, discordant wallets would split evenly; a
469:1 split is astronomically unlikely (McNemar exact test, p < 10⁻¹³⁰).
The interface's default, not a wallet-level taste for convenience or
safety, is what sets the allowance.

## Population

1,019 wallets appear in both arms' populations: the full Uniswap roster
(73,039 wallets) intersected with wallets that entered a swap through one
of the four Sushi routers (5,184) — router entry being the only place a
user faces Sushi's own approval interface. 11 wallets flagged as bots in
either arm are excluded, leaving 1,008; 1,002 of them hold at least one
approval on each side and form the test set. Reproduce with
`python -m dexresearch.process.cross_protocol_overlap`; the per-wallet
list is `datasets/analysis/cross_protocol_wallets.csv`.

## Definitions

- **Uniswap side** — ERC-20 approvals to Permit2 or a Uniswap router
  (`UNI_PATH_SPENDERS`). Uniswap's interface writes this grant as
  unlimited and exposes no amount field; an exact amount here means the
  user went around the interface (a wallet tool, revoke service, or
  manual call).
- **Sushi side** — ERC-20 approvals to the four Sushi routers. Sushi's
  flow exposes the amount, so unlimited is a visible choice.
- **Unlimited** — grant ≥ 2²⁵⁵ (same near-max coalescing as the per-arm
  analyses). A wallet counts as unlimited on a side if *any* of its grants
  there is unlimited; revocations are ignored (the choice at grant time is
  what the interface shaped). The Permit2 second layer (uint160
  sub-allowances) is deliberately not mixed in — this is an ERC-20-layer
  comparison, consistent with the two-layer rule used arm-wide.

## Results

Wallet level (n = 1,002 non-bot wallets with grants on both sides):

| | unlimited on Sushi | exact on Sushi |
|---|---|---|
| **unlimited on Uniswap** | 459 (45.8%) | **469 (46.8%)** |
| **exact on Uniswap** | 1 (0.1%) | 73 (7.3%) |

Unlimited share: **92.6% on the Uniswap path vs 45.9% on Sushi** — the same
people, a 47-point gap.

Held to the same token (n = 1,874 wallet-token pairs approved on both
sides, from 961 wallets) — ruling out token-specific preferences:

| | unlimited on Sushi | exact on Sushi |
|---|---|---|
| **unlimited on Uniswap** | 1,169 (62.4%) | **588 (31.4%)** |
| **exact on Uniswap** | 5 (0.3%) | 112 (6.0%) |

Discordance 588:5; unlimited share 93.8% vs 62.6%. The gap narrows when
the token is held fixed (matched pairs skew toward the majors, where
unlimited is more common on both sides) but the one-sidedness does not.

## Reading it for the policy point

Two facts carry the economic implication:

1. **Defaults dominate.** 92.6% accept unlimited where it is the default;
   under an amount-revealing interface the same wallets choose unlimited
   only 45.9% of the time. If unlimited allowances carry exploit risk,
   roughly half of the exposure on the default-unlimited path is
   interface-manufactured, not user-chosen.
2. **The choice is not symmetric noise.** If wallets had stable private
   preferences and interfaces merely added friction, discordant wallets
   would appear in both directions. They do not (469:1). Preference is
   revealed only where a choice exists; where it doesn't, the default is
   silently absorbed.

Honest framing caveats for the paper:

- On Uniswap's interface, "not opting out" is not a real option — no
  amount field is shown. That *strengthens* the policy reading (the
  default is effectively a mandate for interface users) but the prose
  should not imply Uniswap users declined an offered choice.
- ~46% of these wallets still choose unlimited on Sushi when asked. The
  claim supported is "defaults roughly double unlimited exposure among
  dual-protocol users," not "users reject unlimited when asked."
- The overlap population is more active than either arm's average (they
  cleared two qualifying bars); if anything this biases toward
  sophisticated users and *understates* the default effect for typical
  users.
- Bot exclusion matters little here (11 wallets) but is kept for
  consistency with the arm-level headline numbers.

## The rare direction: exact on Uniswap, unlimited on Sushi

Added 2026-09-19 at Prof. Kim's request for the transactions behind the
"5". `datasets/analysis/cross_protocol_reverse_grants.csv` lists every
grant (block number, tx hash, spender, amount) of the non-bot wallets
that made at least one exact grant on the Uniswap path and at least one
unlimited grant on Sushi: **357 grants from 21 wallets.**

How the counts relate:

- **1 wallet** never granted unlimited on the Uniswap path for any token
  (the wallet-level table above).
- **5 wallet-token pairs** never granted unlimited on the Uniswap path
  for that token (the token-matched table). Rows of these pairs carry
  `pair_strict_reverse = True`.
- **21 wallets** under the loose rule used for this file. Most also
  granted unlimited on Uniswap at some point, so they are mixed users.
  The same loose rule in the common direction gives 584 wallets.

Two cautions when reading the file:

- `huge_exact = True` marks grants of 2⁹⁶−1 or more that fall under the
  2²⁵⁵ unlimited cutoff (for example 2⁹⁶−1 and 2²⁵⁵−1). These are
  unlimited in practice. 2 of the 5 strict pairs, and 2 of the 21
  wallets, are "exact on Uniswap" only because of such grants. The
  classifier is unchanged here so the tables above still reproduce;
  tightening it moves the token-matched discordance from 588:5 to 588:3.
- Order in time matters for any story about users becoming more careful.
  Of the 19 wallets with a real exact Uniswap grant, 12 made all of them
  before their first unlimited Sushi grant, 4 made all of them after, and
  3 are mixed. The common path is from exact toward unlimited, not the
  other way.
