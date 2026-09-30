"""Read-only agent introspection — for the FE 'Network' tab."""

import logging
from fastapi import APIRouter

from core.contracts import agent_signers
from services import chain_reader

log = logging.getLogger("creditgraph.routes.agents")
router = APIRouter()


@router.get("/agents")
async def list_agents():
    """Returns the 3 deterministic Underwriter agents with stake + reputation."""
    out = []
    for a in agent_signers:
        rec = await chain_reader.a_agent_record(a.address)
        out.append(rec or {"address": a.address, "active": False, "role": "Underwriter"})
    return {"agents": out}