# CreditGraph Backend

FastAPI service that operates the CreditGraph protocol on Arbitrum Sepolia: mints identities, runs the 3-agent underwriting quorum, indexes on-chain events, and exposes a single API for the frontend.

**Docs:** [docs](https://creditgraph-be-d21465c875d7.herokuapp.com/api/docs) · **Main repo:** [Main-repo](https://github.com/oadeniran/creditGraph) · **Frontend:** [FE](https://github.com/oadeniran/CreditGraph-FrontEnd)

## What it does

1. **Onboards users** — mints their `CreditIdentity` NFT (admin holds `MINTER_ROLE`)
2. **Runs the underwriting quorum** — three deterministic agents sign an EIP-712 score payload, submit to `ScoringOracle`, then finalize once the challenge window closes
3. **Serves a single dashboard endpoint** that hydrates the entire frontend from live chain state
4. **Indexes on-chain events** in the background (LoanOriginated, Repaid, Defaulted, Attested, Revoked, TierPromoted, etc.) into MongoDB for pagination and historical views
5. **Verifies user-signed transactions** (borrow, repay, attest, supply) — the FE signs via MetaMask, submits the tx hash here, the BE parses the receipt and persists

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                     FastAPI routes                     │
│  /onboard  /borrow  /repay  /attest  /pool  /market   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│                       Services                         │
│    Chain reader · Underwriter quorum · Event indexer   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│                    core/contracts.py                   │
│      Web3 client · signer keys · receipt parsing       │
└──────────┬──────────────────────────────┬──────────────┘
           │                              │
    ┌──────▼──────────┐          ┌────────▼────────┐
    │ Arbitrum Sepolia│          │     MongoDB     │
    │  17 contracts   │          │  event index +  │
    │ canonical state │          │      cache      │
    └─────────────────┘          └─────────────────┘
```

Chain is the source of truth. Mongo is the read-acceleration layer for pagination and history.

## Key features

- **Three deterministic underwriter agents** derived from seeds, registered + staked on-chain at startup
- **EIP-712 quorum signing** — matches `ScoringOracle.SCORE_TYPEHASH` exactly, signatures sorted ascending for on-chain dedup
- **Idempotent bootstrap** — top up agent gas, mint stakes, register, configure challenge period. Safe to re-run.
- **Event indexer** — polls every 15s, upserts on primary keys, resumable from last processed block
- **Optional Robinhood Chain bridge** — writes score mirrors via `BRIDGE_ROLE` (feature-flagged)

## Running locally

**Prerequisites:** Python 3.11+, MongoDB running locally (or a cloud URI).

```bash
# Clone
git clone <TODO>
cd creditgraph-backend

# Set up venv
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate

# Install deps
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set MONGO_URI, RPC_URL, admin key (or leave blank to derive from seed)

# Bootstrap on-chain infra (idempotent)
python -m tools.bootstrap_check --run

# Seed the pool (optional, for demo)
python -m tools.seed_pool 1000

# Run
uvicorn main:app --reload --port 8005
```

Server on `http://localhost:8005`. Interactive docs at `http://localhost:8005/api/docs`.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `MONGO_URI` | Mongo connection | `mongodb://localhost:27017` |
| `RPC_URL` | Arbitrum Sepolia RPC | `https://sepolia-rollup.arbitrum.io/rpc` |
| `CHAIN_ID` | Chain ID | `421614` |
| `DEPLOYER_PRIVATE_KEY` | Admin signing key (optional; overrides ADMIN_SEED) | — |
| `ADMIN_SEED` | Deterministic admin seed (fallback) | `creditgraph-admin-v1` |
| `AGENT_SEED_1..3` | Deterministic agent seeds | see .env.example |
| `AGENT_STAKE_USDC` | Per-agent stake | `10` |
| `TRY_SET_CHALLENGE_PERIOD_ZERO` | Zero out challenge window at startup | `true` |
| `INDEXER_ENABLED` | Run background event indexer | `true` |
| `INDEXER_POLL_SECONDS` | Poll interval | `15` |
| `USDC_ADDRESS` etc. | Deployed contract addresses | Arbitrum Sepolia defaults |

Full list in `.env.example`.

## API surface

| Route | Purpose |
|---|---|
| `POST /api/onboard` | Mint identity + run first underwriting pass |
| `GET  /api/user/{wallet}` | Everything the frontend needs, in one call |
| `POST /api/borrow` | Verify a FE-submitted `LoanManager.originate` tx |
| `POST /api/repay` | Verify a repay tx |
| `POST /api/attest` | Verify an attest tx |
| `POST /api/attest/revoke` | Two-step revoke (`request` then `finalize`) |
| `POST /api/pool/supply` | Verify a supply tx |
| `POST /api/pool/withdraw` | Verify a withdraw tx |
| `GET  /api/pool/stats` | ERC-4626 pool state |
| `GET  /api/pool/apr-preview` | Live APR for a given tier + utilization |
| `GET  /api/market/overview` | TVL, treasury, insurance, agent network |
| `POST /api/score/underwrite` | Trigger a fresh underwriting pass |
| `POST /api/score/finalize` | Finalize a pending score after the challenge window |
| `GET  /api/score/reason/{hash}` | Full "Why this score?" payload |
| `POST /api/x402/demo-open` | Open an agent-to-agent x402 demo channel |
| `POST /api/x402/demo-settle` | Settle a cumulative voucher on-chain |
| `POST /api/faucet` | Mint test USDC to any wallet |
| `GET  /api/health` | Diagnostics: connection, admin balance, agent status |

## Tools

```bash
# Verify + optionally run bootstrap (idempotent)
python -m tools.bootstrap_check [--run]

# Local sanity check: EIP-712 signing dry run, zero gas
python -m tools.test_signing

# Seed the LendingPool with test USDC
python -m tools.seed_pool [amount_usdc]
```

## Stack

- FastAPI + uvicorn (async)
- MongoDB via Motor
- web3.py + eth_account
- Deterministic agents via `hashlib.sha256(seed)` → `LocalAccount`