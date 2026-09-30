from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


# ----------------------------------------------------------------
# Onboarding & identity
# ----------------------------------------------------------------

class UserOnboardRequest(BaseModel):
    wallet_address: str


class MintIdentityRequest(BaseModel):
    wallet_address: str
    metadata_hash: Optional[str] = None  # 0x-prefixed bytes32; auto-generated if omitted


# ----------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------

class UnderwriteRequest(BaseModel):
    wallet_address: str


class FinalizeScoreRequest(BaseModel):
    token_id: int


class ChallengeScoreRequest(BaseModel):
    token_id: int


# ----------------------------------------------------------------
# Lending
# ----------------------------------------------------------------

class BorrowRequest(BaseModel):
    """FE submits this AFTER the user has signed LoanManager.originate via MetaMask."""
    wallet_address: str
    tx_hash: str


class RepayRequest(BaseModel):
    """FE submits this AFTER the user has signed LoanManager.repay via MetaMask."""
    wallet_address: str
    tx_hash: str


class MarkLateRequest(BaseModel):
    loan_id: int


class MarkDefaultRequest(BaseModel):
    loan_id: int


# ----------------------------------------------------------------
# Social attestation
# ----------------------------------------------------------------

class AttestRequest(BaseModel):
    """FE submits this AFTER the user has signed SocialAttestation.attest via MetaMask."""
    tx_hash: str


class AttestRevokeRequest(BaseModel):
    attestation_id: int
    tx_hash: str
    action: str  # "request" or "finalize"


# ----------------------------------------------------------------
# Pool (ERC-4626)
# ----------------------------------------------------------------

class SupplyRequest(BaseModel):
    wallet_address: str
    tx_hash: str


class WithdrawRequest(BaseModel):
    wallet_address: str
    tx_hash: str


# ----------------------------------------------------------------
# x402
# ----------------------------------------------------------------

class OpenChannelRequest(BaseModel):
    tx_hash: str


class SettleChannelRequest(BaseModel):
    channel_id: str
    cumulative_amount: int  # USDC units (6 decimals)


class CloseChannelRequest(BaseModel):
    channel_id: str
    tx_hash: str


# ----------------------------------------------------------------
# Faucet
# ----------------------------------------------------------------

class FaucetRequest(BaseModel):
    wallet_address: str
    amount_usdc: float = 100.0


# ----------------------------------------------------------------
# Response shapes (loose so chain reads can return any extras)
# ----------------------------------------------------------------

class ScoreData(BaseModel):
    score: int
    tier: int
    is_stale: bool
    has_score: bool
    reason_hash: Optional[str] = None


class LoanData(BaseModel):
    loan_id: int
    token_id: int
    principal_usdc: float
    outstanding_principal_usdc: float
    outstanding_total_usdc: float
    interest_paid_usdc: float
    originated_at: int
    due_at: int
    apr_bps: int
    state: str
    state_code: int


class AttestationData(BaseModel):
    attestation_id: Optional[int] = None
    attester_token_id: int
    subject_token_id: int
    attester_address: Optional[str] = None
    subject_address: Optional[str] = None
    bond_usdc: float
    created_at: int
    expires_at: int
    active: bool
    relationship_type: str
    revoke_unlock_at: Optional[int] = None


class GraduationData(BaseModel):
    tier: int
    streak: int
    lifetime_on_time: int
    lifetime_defaults: int
    thresholds: List[int]
    next_tier_threshold: Optional[int]
    to_next_tier: Optional[int]


class DashboardResponse(BaseModel):
    wallet_address: str
    token_id: int
    has_identity: bool
    credit_score: ScoreData
    pending_score: Optional[Dict[str, Any]] = None
    challenge_period: int
    available_limit_usdc: float
    current_exposure_usdc: float
    headroom_usdc: float
    attestation_weight_usdc: float
    graduation: GraduationData
    active_loans: List[LoanData]
    attestations_received: List[AttestationData]
    attestations_given: List[AttestationData]
    usdc_balance: float
    cgusdc_shares: int
    cgusdc_assets_usdc: float


class PoolStatsResponse(BaseModel):
    total_assets_usdc: float
    total_borrowed_usdc: float
    available_liquidity_usdc: float
    utilization_bps: int
    utilization_pct: float
    cumulative_interest_usdc: float
    cumulative_losses_usdc: float
    supply_cap_usdc: float
    reference_borrow_apr_bps: int
    supply_apr_bps: int