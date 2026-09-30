"""
CreditGraph FastAPI entrypoint.

On startup:
  1. Ensure DB indexes
  2. Run on-chain bootstrap (register agents, set min stake, zero challenge period if possible)
  3. Start background event indexer

Mount all routers under /api.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.database import ensure_indexes
from core.bootstrap import run_bootstrap
from services.indexer import run_indexer

from api.routes import (
    identity, scoring, dashboard, loans, attest, pool,
    x402, agents, admin, market,
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
    bootstrap_status = run_bootstrap()
    app.state.bootstrap = bootstrap_status

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
    version="0.2",
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

# Mount routers
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
    return {"message": "CreditGraph API. LFG."}


@app.get("/api/health")
async def health():
    """Diagnostics. Useful when something looks off."""
    from core.contracts import admin_signer, agent_signers, w3, is_connected
    from services import chain_reader

    try:
        admin_eth = w3.eth.get_balance(admin_signer.address) / 1e18
    except Exception:
        admin_eth = None

    agent_status = []
    for a in agent_signers:
        rec = chain_reader.agent_record(a.address)
        agent_status.append({
            "address": a.address,
            "active": rec.get("active", False) if rec else False,
            "stake_usdc": rec.get("stake_usdc", 0) if rec else 0,
        })

    return {
        "connected": is_connected(),
        "chain_id": w3.eth.chain_id if is_connected() else None,
        "admin_address": admin_signer.address,
        "admin_eth": admin_eth,
        "agents": agent_status,
        "challenge_period": chain_reader.oracle_challenge_period(),
        "bootstrap": getattr(app.state, "bootstrap", None),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)