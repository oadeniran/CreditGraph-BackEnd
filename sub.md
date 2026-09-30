envs.py
```python
from dotenv import load_dotenv
import os

load_dotenv()  # Load environment variables from .env file

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
RPC_URL = os.getenv("RPC_URL", "https://sepolia-rollup.arbitrum.io/rpc")
```


main.py
```python
# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import onboard, dashboard, transactions

app = FastAPI(title="CreditGraph API", version="0.1", docs_url="/api/docs",)

# Allow Next.js frontend to call us
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Change to localhost:3000 in prod if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(onboard.router, prefix="/api", tags=["Onboarding"])
app.include_router(dashboard.router, prefix="/api", tags=["Dashboard"])
app.include_router(transactions.router, prefix="/api", tags=["Transactions"])

@app.get("/")
async def root():
    return {"message": "CreditGraph API running. LFG."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
```


requirements.txt
```text
fastapi
uvicorn
motor
pydantic
pydantic-settings
python-dotenv
web3
```


api/routes/dashboard.py
```python
# app/api/routes/dashboard.py
from fastapi import APIRouter, HTTPException, Query
from models.schemas import DashboardResponse, ScoreData
from core.database import db
from services.agent_service import calculate_credit_limit
import math

router = APIRouter()

@router.get("/user/{wallet_address}", response_model=DashboardResponse)
async def get_dashboard(wallet_address: str):
    """The mega-endpoint that hydrates the Next.js FE in one call."""
    
    user = await db.users.find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Call /onboard first.")

    score = await db.scores.find_one({"wallet_address": wallet_address})
    loans_cursor = db.loans.find({"wallet_address": wallet_address, "state": {"$in": ["Active", "Late"]}})
    loans = await loans_cursor.to_list(length=100)
    
    attests_cursor = db.attests.find({"subject_address": wallet_address, "active": True})
    attests = await attests_cursor.to_list(length=100)

    # Limit Engine Math
    total_limit = await calculate_credit_limit(wallet_address, score["tier"])
    exposure = sum(loan["outstanding"] for loan in loans)
    headroom = max(0, total_limit - exposure)

    return {
        "wallet_address": wallet_address,
        "token_id": user["token_id"],
        "credit_score": {
            "score": score["score"],
            "tier": score["tier"],
            "reason": score["reason"],
            "updated_at": score["updated_at"]
        },
        "available_limit": total_limit,
        "current_exposure": exposure,
        "headroom": headroom,
        "active_loans": [
            {
                "loan_id": str(l["_id"]), 
                "principal": l["principal"], 
                "outstanding": l["outstanding"],
                "apr_bps": l["apr_bps"],
                "due_at": l["due_at"],
                "state": l["state"]
            } for l in loans
        ],
        "active_attestations": [
            {
                "attester_address": a["attester_address"],
                "bond_amount": a["bond_amount"],
                "active": a["active"]
            } for a in attests
        ]
    }

@router.get("/user/{wallet_address}/loans")
async def get_loan_history(
    wallet_address: str, 
    page: int = Query(1, ge=1), 
    size: int = Query(5, ge=1) # Defaulting to 5 per page for the demo
):
    skip = (page - 1) * size
    
    # Get total for pagination math
    total_count = await db.loans.count_documents({"wallet_address": wallet_address})
    total_pages = math.ceil(total_count / size) if total_count > 0 else 1

    cursor = db.loans.find({"wallet_address": wallet_address}).sort("originated_at", -1).skip(skip).limit(size)
    loans = await cursor.to_list(length=size)
    
    return {
        "items": [
            {
                "loan_id": str(l["_id"]), 
                "principal": l["principal"], 
                "outstanding": l["outstanding"],
                "apr_bps": l.get("apr_bps", 1500),
                "due_at": l["due_at"],
                "state": l["state"]
            } for l in loans
        ],
        "total_pages": total_pages,
        "current_page": page
    }

@router.get("/user/{wallet_address}/attestations")
async def get_attestation_history(
    wallet_address: str,
    page: int = Query(1, ge=1), 
    size: int = Query(5, ge=1)
):
    skip = (page - 1) * size
    
    total_count = await db.attests.count_documents({"attester_address": wallet_address})
    total_pages = math.ceil(total_count / size) if total_count > 0 else 1

    cursor = db.attests.find({"attester_address": wallet_address}).sort("created_at", -1).skip(skip).limit(size)
    attests = await cursor.to_list(length=size)
    
    return {
        "items": [
            {
                "attester_address": a["attester_address"],
                "subject_address": a.get("subject_address", "Unknown"),
                "bond_amount": a["bond_amount"],
                "active": a["active"],
                "created_at": a.get("created_at")
            } for a in attests
        ],
        "total_pages": total_pages,
        "current_page": page
    }
```


api/routes/demo.py
```python
```


api/routes/onboard.py
```python
# app/api/routes/onboard.py
from fastapi import APIRouter
from models.schemas import UserOnboardRequest
from services.agent_service import mint_identity_and_score

router = APIRouter()

@router.post("/onboard")
async def onboard_user(req: UserOnboardRequest):
    """Simulates wallet connect, minting soulbound ID, and initial agent scoring."""
    user = await mint_identity_and_score(req.wallet_address)
    return {"message": "Identity Minted & Scored", "token_id": user["token_id"]}
```


api/routes/transactions.py
```python
# app/api/routes/transactions.py
from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta
from bson.objectid import ObjectId

from models.schemas import BorrowRequest, RepayRequest, AttestRequest
from core.database import db
from core.web3_utils import verify_transaction

router = APIRouter()

@router.post("/borrow")
async def borrow_funds(req: BorrowRequest):
    """Originates a loan after FE completes the MetaMask transaction."""
    
    # 1. Try to verify on-chain (Fallback to True if RPC fails)
    if not verify_transaction(req.tx_hash):
        raise HTTPException(status_code=400, detail="Transaction reverted on-chain.")

    # 2. Check user exists
    user = await db.users.find_one({"wallet_address": req.wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # 3. Create Loan Record in Mongo
    # Note: In a full app, we'd pull apr_bps from the InterestRateModel SC. 
    # Here we mock a 15% APR (1500 bps) for the demo.
    loan_doc = {
        "wallet_address": req.wallet_address,
        "principal": req.amount,
        "outstanding": req.amount,
        "apr_bps": 1500,
        "originated_at": datetime.utcnow(),
        "due_at": datetime.utcnow() + timedelta(days=req.term_days),
        "state": "Active",
        "tx_hash": req.tx_hash
    }
    
    result = await db.loans.insert_one(loan_doc)
    
    return {
        "message": "Loan originated successfully", 
        "loan_id": str(result.inserted_id)
    }

@router.post("/repay")
async def repay_loan(req: RepayRequest):
    """Processes a loan repayment."""
    
    if not verify_transaction(req.tx_hash):
        raise HTTPException(status_code=400, detail="Transaction reverted on-chain.")

    # Find the loan
    loan = await db.loans.find_one({"_id": ObjectId(req.loan_id), "wallet_address": req.wallet_address})
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found.")

    # Calculate new outstanding balance
    new_outstanding = max(0.0, loan["outstanding"] - req.amount)
    new_state = "Repaid" if new_outstanding == 0 else "Active"

    # Update Mongo
    await db.loans.update_one(
        {"_id": ObjectId(req.loan_id)},
        {"$set": {
            "outstanding": new_outstanding,
            "state": new_state,
            "last_repayment_at": datetime.utcnow()
        }}
    )

    return {"message": f"Repayment successful. New balance: {new_outstanding}", "state": new_state}

@router.post("/attest")
async def social_attestation(req: AttestRequest):
    """The 'Vouch for a Friend' flow (SocialAttestation.sol)"""
    
    if not verify_transaction(req.tx_hash):
        raise HTTPException(status_code=400, detail="Transaction reverted on-chain.")

    # Verify subject exists
    subject = await db.users.find_one({"wallet_address": req.subject_address})
    if not subject:
        raise HTTPException(status_code=404, detail="Subject user does not exist in CreditGraph.")

    # Record the attestation
    attest_doc = {
        "attester_address": req.attester_address,
        "subject_address": req.subject_address,
        "bond_amount": req.bond_amount,
        "active": True,
        "created_at": datetime.utcnow(),
        "tx_hash": req.tx_hash
    }
    
    await db.attests.insert_one(attest_doc)

    return {"message": f"Successfully vouched for {req.subject_address} with {req.bond_amount} USDC."}
```


core/config.py
```python
```


core/database.py
```python
# app/core/database.py
from motor.motor_asyncio import AsyncIOMotorClient
from envs import MONGO_URI


client = AsyncIOMotorClient(MONGO_URI)
db = client.creditgraph_db

# Collections mimicking our smart contracts
users_collection = db.users           # CreditIdentity
scores_collection = db.scores         # ScoreRegistry
loans_collection = db.loans           # LoanManager
attestations_collection = db.attests  # SocialAttestation
```


core/web3_utils.py
```python
# app/core/web3_utils.py
import os
from web3 import Web3
from web3.exceptions import Web3Exception
from envs import RPC_URL

w3 = Web3(Web3.HTTPProvider(RPC_URL))

def verify_transaction(tx_hash: str) -> bool:
    """
    Tries to verify a transaction on-chain.
    Returns True if successful OR if the RPC fails (the hackathon fallback).
    Returns False ONLY if the transaction explicitly reverted on-chain.
    """
    if not tx_hash or tx_hash == "mock_hash":
        return True # Trust the frontend for mock/fallback flows

    try:
        # Give it a short timeout so the UI doesn't hang if the network is slow
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=5)
        # status == 1 means success in EVM
        return receipt.status == 1
    except Web3Exception as e:
        print(f"Web3 Error (Falling back to Mongo): {e}")
        # HACKATHON MAGIC: If the RPC fails, we pretend it worked to save the demo.
        return True
    except Exception as e:
        print(f"Unexpected Error: {e}")
        return True
```


models/schemas.py
```python
# app/models/schemas.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class UserOnboardRequest(BaseModel):
    wallet_address: str

class ScoreData(BaseModel):
    score: int
    tier: int
    reason: str
    updated_at: datetime

class LoanData(BaseModel):
    loan_id: str
    principal: float
    outstanding: float
    apr_bps: int
    due_at: datetime
    state: str # "Active", "Repaid", "Late", "Defaulted"

class AttestationData(BaseModel):
    attester_address: str
    bond_amount: float
    active: bool

class DashboardResponse(BaseModel):
    wallet_address: str
    token_id: int
    credit_score: ScoreData
    available_limit: float
    current_exposure: float
    headroom: float
    active_loans: List[LoanData]
    active_attestations: List[AttestationData]

class BorrowRequest(BaseModel):
    wallet_address: str
    amount: float
    term_days: int
    tx_hash: Optional[str] = "mock_hash"

class RepayRequest(BaseModel):
    wallet_address: str
    loan_id: str
    amount: float
    tx_hash: Optional[str] = "mock_hash"

class AttestRequest(BaseModel):
    attester_address: str
    subject_address: str # The friend they are vouching for
    bond_amount: float
    tx_hash: Optional[str] = "mock_hash"
```


services/agent_service.py
```python
# app/services/agent_service.py
import random
from datetime import datetime
from core.database import db

async def mint_identity_and_score(wallet_address: str):
    """Simulates CreditIdentity.mint() and DataCollector/Underwriter Agents"""
    
    # Check if user exists
    existing = await db.users.find_one({"wallet_address": wallet_address})
    if existing:
        return existing

    # Mock Agent Scoring Logic
    mock_score = random.randint(600, 750)
    tier = 3 if mock_score > 700 else 2
    
    user_doc = {
        "wallet_address": wallet_address,
        "token_id": random.randint(1000, 9999), # Fake ERC-5192 Token ID
        "created_at": datetime.utcnow()
    }
    await db.users.insert_one(user_doc)

    score_doc = {
        "wallet_address": wallet_address,
        "score": mock_score,
        "tier": tier,
        "reason": "Agent verified mobile money inflows > $50/mo",
        "updated_at": datetime.utcnow()
    }
    await db.scores.insert_one(score_doc)
    
    return user_doc

async def calculate_credit_limit(wallet_address: str, tier: int):
    """Simulates CreditLimitEngine.sol"""
    # Base limits by tier
    tier_limits = {1: 20, 2: 50, 3: 150, 4: 500, 5: 2000}
    base = tier_limits.get(tier, 20)
    
    # Calculate social attestation bonus (Cap at 2x base)
    cursor = db.attests.find({"subject_address": wallet_address, "active": True})
    attestations = await cursor.to_list(length=100)
    
    bonus = sum(a["bond_amount"] for a in attestations)
    max_bonus = base * 2
    
    return base + min(bonus, max_bonus)
```


services/lending_service.py
```python
```


