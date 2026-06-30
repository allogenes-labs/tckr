# tckr keyless coverage — blind-spot report

**Date:** 2026-06-29 · **Mode:** no API keys (default install) · **Method:** hybrid —
a reproducible tool-battery harness (`keyless_coverage.py`) across 17 assets, plus
3 agent-driven HTML reports (HYPE, ANSEM, MU) under `reports/` in the run output.

This report measures how well tckr serves the real prompt *"run analysis on X
asset, create a short HTML report"* when an agent drives it with **no keys** —
where the cascade delivers, where it goes dark, and (most importantly) where it
returns **confident but wrong** data. No code was changed; §5 lists prioritized
fixes for a later pass.

See `matrix.md` for the full asset × capability grid and provider-health table.

---

## 1. Headline

The keyless baseline is **genuinely strong for crypto majors/mid-caps** and
**surprisingly capable for US equity/ETF spot + options** — but it has three
classes of blind spot, in order of danger:

1. **Silent wrong-asset resolution** (most dangerous): the crypto `quote`/
   `candles`/`ta_*` cascade has no asset-class awareness and will return a
   *same-ticker crypto or tokenized proxy* for a non-crypto query, presented as
   success with no flag.
2. **No keyless technical analysis off-crypto, and meaningless TA on young
   pools**: `ta_*` only reads the crypto candle cascade.
3. **Coverage cliffs**: per-token crypto news, Solana token security/holders, and
   tradfi fundamentals are unavailable keyless.

A coverage `✓` in the matrix means "data came back", **not** "data is correct" —
the gap between those two is the core finding.

---

## 2. What works keyless (the wins)

- **Crypto majors & mid-caps (BTC/ETH/SOL/HYPE/RUNE):** full stack — live price,
  daily candles, risk stats, all technical indicators, perp funding/OI, oracle
  cross-check, symbol search, and (for majors) news. Hyperliquid is the primary
  for its ~230-symbol universe (no rate limit), CoinGecko backstops the long tail.
  The **HYPE report** is the model outcome: nothing degraded.
- **Long-tail / memecoins on CoinGecko (MOG/ANSEM/MICHI/DEGEN):** price + candles
  + TA resolve via CoinGecko; DEX tools (`ds_search`, `gt_token_info`,
  `gt_pool_ohlcv`) give pool price, FDV, market cap, volume, and OHLCV keyless.
- **US equity/ETF spot — Pyth oracle:** `py_latest_price` returned correct prices
  for MU, AAPL, NVDA, SPY and metals XAU/XAG. **Pyth is the keyless hero for
  non-crypto spot** and the natural cross-check against the crypto cascade.
- **US equity/ETF options — CBOE:** `opt_chain`/`opt_expirations` returned real
  chains keyless (MU 415 contracts/19 expiries, SPY 34 expiries, AAPL 25, NVDA
  24) with greeks + IV on near-the-money strikes. A real strength.
- **Prediction markets (Polymarket)** and **macro/global news (GDELT,** when not
  rate-limited**)** work with no keys.

---

## 3. Blind spots (with evidence)

### 3.1 Silent wrong-asset resolution — `quote` / `candles` / `ta_*`  ⛔ highest priority
The cascade is crypto-only and resolves any ticker through CoinGecko's
`coin_id_from_symbol`. For a non-crypto ticker the outcome is **silent and
inconsistent**:

| ticker | `quote` returned | reality (Pyth) | verdict |
|---|---|---|---|
| **XAU** (gold) | $0.0000539 — a microcap "gold" token | **$4,018** | ✗ off by ~8 orders of magnitude |
| **SPY** (S&P ETF) | $0.000213 — "SmartyPay" token | **$736** | ✗ unrelated asset |
| **MU** (Micron) | $1,050 — `micron-technology-backpack-securities` (tokenized stock) | $1,060 | ⚠ proxy that *happens* to track, unflagged |
| AAPL / NVDA / WTI | unresolved (empty) | — | ✓ safe (no false match) |

The agent receives a plausible number either way and **cannot tell** the
catastrophic case (XAU/SPY) from the lucky case (MU) from the safe case (AAPL).
The audit's "don't resolve unknown → unrelated coin" fix holds for *truly*
unknown tickers (AAPL/NVDA/WTI → empty) but does nothing for tickers that
collide with a real crypto/tokenized token. **The fix is asset-class awareness +
a Pyth cross-check, not a bigger blocklist.**

### 3.2 No keyless TA off-crypto; meaningless TA on young pools
- `ta_risk`/`ta_indicators` read only the crypto candle cascade → for real
  equities/commodities they return `"insufficient candle history"` (AAPL, NVDA,
  XAG, WTI) or compute on the wrong proxy (MU: 8 bars → cum −14%, "vol" 158%,
  Sharpe −4.4 — garbage).
- For **freshly-launched memecoins** the pool OHLCV exists but spans days, so the
  primitives produce nonsense: ANSEM DEX-pool TA = **+40,386% cumulative,
  25,639% annualized vol**, beta vs BTC **175**, RSI undefined. No guardrail
  flags this.

### 3.3 Memecoin ticker ambiguity — no resolution signal
`ds_search("ANSEM")` returned **15 distinct token contracts** on Solana sharing
the symbol (plus ≥5 CoinGecko listings, e.g. `the-black-bull`, `ansem-army`,
`soylanamanletcaptainz`). CoinGecko silently picks one; the agent isn't told
there are 14 others, or that a separate token literally named "Ansem" (2024)
exists at a different address. The harness's `resolve_meme_pair` (rank by
liquidity) is a working keyless disambiguation prototype that the toolkit does
not currently expose.

### 3.4 Coverage cliffs (genuinely unavailable keyless)
- **Per-token crypto news:** `·` for RUNE, MOG, ANSEM, MICHI, DEGEN. Keyless
  crypto news is outlet-RSS headlines only — even HYPE returned 0 on a
  token-specific query. Structural.
- **Solana token security & holders:** no keyless path. GoPlus/Honeypot are
  EVM-only *and* not even exposed as agent tools (see 3.5). For a memecoin the
  missing honeypot/holder check is the single most important gap — and absence
  reads as "unknown", which must not be mistaken for "safe".
- **Tradfi fundamentals** (revenue/EPS/valuation): none keyless.
- **WTI/oil oracle:** Pyth returned no feed for `WTI/USD`. *(Resolved post-audit:
  a commodity classification fallback now routes WTI/Brent/NatGas to Yahoo —
  `quote` and `ta_*` answer keyless. See §5.)*

### 3.5 Cross-cutting issues
- **Security tools missing from the toolkit:** `goplus` and `honeypot` are
  keyless-configured in the registry but are **not among the 67 exposed agent
  tools** — an agent can't run a contract security scan at all.
- **Tool-description drift:** the `quote` tool description says "cascading
  CoinGecko → Hyperliquid" but the implementation (`tckr/quotes.py`) is
  **Hyperliquid → CoinGecko**. Misleads the agent about which source it's getting.
- **GDELT is rate-limit-fragile keyless:** 31 of 47 calls 429'd this run. Because
  the unified `news` cascade leans on GDELT for non-crypto, tradfi news is
  unreliable under load (most tradfi-`news`/`gdelt` empties are transient, per the
  health snapshot — *not* missing coverage).
- **Pyth resolution prefers crypto:** `feed_id_for_symbol` prefers a crypto feed
  on a symbol collision — fine here (MU/AAPL/NVDA resolved correctly) but a
  latent risk for any equity ticker that also exists as a Pyth crypto feed.

---

## 4. Per-archetype scorecard (keyless)

| Archetype | Spot | History/TA | Derivatives | News | Security | Overall |
|---|---|---|---|---|---|---|
| Major/mid crypto | ✓ | ✓ | ✓ perps | ✓ majors / · token | n/a | **Excellent** |
| Long-tail crypto | ✓ | ✓ (CG) | · | · | n/a | Good |
| Memecoin (Sol/Base) | ✓ DEX | ⚠ nonsense on young pools | n/a | ✗ | ✗ keyless | **Partial + risky** |
| US equity / ETF | ✓ Pyth | ✗ no keyless history | ✓ CBOE options | ⚠ GDELT-only | n/a | Good (spot+options) |
| Commodity | ✓ Pyth (metals) | ✗ | ✗ | ⚠ | n/a | Spot-only |
| Prediction | ✓ | n/a | ✓ | n/a | n/a | Good |

---

## 5. Recommendations (prioritized; documented, not implemented)

**P1 — Asset-class awareness + cross-check for `quote`/`candles`/`ta_*`.**
Detect or accept a hint for non-crypto tickers; route equities/commodities to
Pyth (and CBOE for options) instead of the crypto cascade; when only the crypto
cascade answers a likely-tradfi ticker, **tag the source as a same-ticker
crypto / tokenized proxy** and/or cross-check against Pyth and warn on large
divergence. *Deferred decision (per plan):* whether to add a **keyless equity/
commodity daily-history source (e.g. Stooq CSV)** so `ta_*` works for MU/Gold,
vs. **graceful degradation** ("no keyless history for this asset class; here's
Pyth spot + CBOE options"). The evidence says the divergence problem (3.1) is the
more urgent half — fix the wrong-asset silence first.

**P2 — Memecoin symbol→contract resolution.** Expose a keyless resolver
(`ds_search`/`gt` ranked by liquidity — prototype already in
`keyless_coverage.py:resolve_meme_pair`), let `quote`/`ta_*` accept a contract
address, and **return the ambiguity** (N same-symbol tokens) instead of silently
choosing one.

**P3 — TA guardrails for short/young series.** Suppress or flag annualized stats
(vol, Sharpe, beta) when `n_bars` < threshold or the series spans < N days; cap
absurd values. Add a `data_quality` field so the agent can refuse to render
nonsense (ANSEM-style).

**P4 — Degradation messaging + capabilities hints.** Replace the opaque
`"insufficient candle history"` with asset-class-aware guidance ("no keyless
equity history; use `py_latest_price` for spot and `opt_chain` for options").
Add `capabilities`/tool-doc hints steering agents to Pyth for non-crypto spot and
CBOE for options.

**P5 — Expose security tools; document Solana security gap.** Surface
`goplus`/`honeypot` as agent tools, and explicitly document that Solana token
security/holders have no keyless path (so absence is read as "unknown").

**P6 — News robustness.** Harden GDELT keyless use (longer cache, backoff, dedupe
queries); document that per-token/per-ticker news is thin keyless; recommend the
free `FINNHUB_API_KEY`.

**P7 — Fix `quote` tool-description drift** (CG→HL vs actual HL→CG). One-line doc fix.

### Resolution status (all implemented — see CHANGELOG `[Unreleased]`)

| # | Status | What landed |
|---|---|---|
| P1 | ✅ | `quote` routes by asset class (address→DexScreener, HL ticker→Hyperliquid, non-crypto→Pyth, commodities Pyth lacks→Yahoo `spot`, else CoinGecko) with `asset_class` + `warning`; new `tckr.yahoo` keyless history so `ta_*` work off-crypto (incl. WTI/Brent/NatGas via a commodity fallback); non-crypto history never falls back to CoinGecko. |
| P2 | ✅ | `token_resolve` tool (ranked, deduped, `ambiguous`/`n_distinct_tokens`); `quote` accepts a contract address. |
| P3 | ✅ | `ta_*` suppress annualized stats + emit `warnings`/`data_quality` on short (<30-bar) or launch-ramp series; tradfi annualizes on 252. |
| P4 | ✅ | Asset-class-aware "no keyless history" messages; `capabilities`/`render_tools_doc` usage hints. |
| P5 | ✅ | `security_token` (GoPlus) + `honeypot_check` exposed as agent tools. |
| P6 | ✅ | Process-wide GDELT rate gate (`GDELT_MIN_INTERVAL_S`, default 5s; end-to-end spacing). Verified deterministically in `tests/test_assetclass_routing.py` (mocked HTTP) since the live endpoint soft-blocks under repeated testing. |
| P7 | ✅ | `quote` description corrected to HL-first. |

Post-fix verification: `quote('XAU')`→Pyth metal $3,964 (was a $0.00005 token);
`ta_risk('MU')`→Yahoo, 124 bars, 90% vol, `reliable=true` (was an 8-bar proxy);
`token_resolve('ANSEM')`→19 distinct tokens, `ambiguous=true`; young-pool TA
returns `reliable=false` + warnings; `quote('WTI')`→Yahoo $70.85 + 120-bar TA
(was unresolved). Full suite green (~115 passed; a few live keyless-smoke tests
skip when upstreams are flaky).

---

## 6. Reproducing this

```bash
# from repo root, in the project venv (keyless: the harness scrubs key env vars itself)
python tests/coverage/keyless_coverage.py <out_dir>
# tune politeness for free-tier rate limits:
COV_SLEEP=2.0 python tests/coverage/keyless_coverage.py <out_dir>
```
Outputs: `<out_dir>/matrix.{md,csv}`, `<out_dir>/raw/<ASSET>.json` (per-probe
status/source/note), `<out_dir>/meta.json` (capabilities summary + provider
health). The 3 example HTML reports (HYPE, MU, ANSEM) live in
`tests/coverage/reports/` and were authored by an agent composing the same tool
callables — MU and ANSEM reflect the post-fix behavior (Pyth+Yahoo+CBOE for MU;
`token_resolve` + guardrails for ANSEM).
