"""
CreditGraph BE entrypoint.

Startup:
  1. Ensure DB indexes
  2. Bootstrap all configured chains
  3. Spawn per-chain event indexers
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.database import ensure_indexes
from core.bootstrap import bootstrap_all_configured_chains
from services.indexer import run_indexer

from api.routes import (
    identity, scoring, dashboard, loans, attest, pool,
    x402, agents, admin, market, chains,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("creditgraph")

_indexer_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("--- CreditGraph BE starting ---")
    await ensure_indexes()

    bootstrap_results = bootstrap_all_configured_chains()
    app.state.bootstrap = bootstrap_results

    global _indexer_task
    _indexer_task = asyncio.create_task(run_indexer())

    yield

    log.info("--- CreditGraph BE shutting down ---")
    if _indexer_task:
        _indexer_task.cancel()
        try:
            await _indexer_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="CreditGraph API",
    version="0.3",
    docs_url="/api/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chains.router, prefix="/api", tags=["Chains"])
app.include_router(identity.router, prefix="/api", tags=["Identity"])
app.include_router(scoring.router, prefix="/api", tags=["Scoring"])
app.include_router(dashboard.router, prefix="/api", tags=["Dashboard"])
app.include_router(loans.router, prefix="/api", tags=["Loans"])
app.include_router(attest.router, prefix="/api", tags=["Attestations"])
app.include_router(pool.router, prefix="/api", tags=["Pool"])
app.include_router(x402.router, prefix="/api", tags=["x402"])
app.include_router(agents.router, prefix="/api", tags=["Agents"])
app.include_router(admin.router, prefix="/api", tags=["Admin"])
app.include_router(market.router, prefix="/api", tags=["Market"])


@app.get("/")
async def root():
    return {"message": "CreditGraph API — multi-chain"}


@app.get("/api/health")
async def health():
    from core.contracts import admin_signer, agent_signers, get_w3
    from core.chains import list_chains
    from services import chain_reader

    per_chain = []
    for chain_meta in list_chains():
        key = chain_meta["key"]
        try:
            w3 = get_w3(key)
            connected = w3.is_connected() and w3.eth.chain_id == chain_meta["chain_id"]
            admin_native = w3.eth.get_balance(admin_signer.address) / 1e18
        except Exception:
            connected = False
            admin_native = None

        agent_status = []
        for a in agent_signers:
            rec = chain_reader.agent_record(key, a.address)
            agent_status.append({
                "address": a.address,
                "active": rec.get("active", False) if rec else False,
                "stake_usdc": rec.get("stake_usdc", 0) if rec else 0,
            })

        per_chain.append({
            "chain_key": key,
            "name": chain_meta["name"],
            "chain_id": chain_meta["chain_id"],
            "connected": connected,
            "admin_native": admin_native,
            "agents": agent_status,
            "challenge_period": chain_reader.oracle_challenge_period(key),
        })

    return {
        "admin_address": admin_signer.address,
        "chains": per_chain,
        "bootstrap": getattr(app.state, "bootstrap", None),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)