"""
Read-only chain queries. Anything the FE needs to display goes through here.
Mongo is consulted afterward for things the chain can't easily give us
(human history, pagination, friendly metadata).
"""

import logging
from typing import Optional
from web3 import Web3
from core.contracts import contracts, get
import asyncio

log = logging.getLogger("creditgraph.chain_reader")

USDC_DECIMALS = 6



# Async wrappers — sync versions stay for non-async callers.
# Pattern: `await async_get_score(token_id)` instead of `get_score(token_id)`.
async def a_token_id_of(wallet):           return await asyncio.to_thread(token_id_of, wallet)
async def a_get_score(token_id):           return await asyncio.to_thread(get_score, token_id)
async def a_get_pending_score(token_id):   return await asyncio.to_thread(get_pending_score, token_id)
async def a_oracle_challenge_period():     return await asyncio.to_thread(oracle_challenge_period)
async def a_graduation_state(token_id):    return await asyncio.to_thread(graduation_state, token_id)
async def a_available_credit(token_id):    return await asyncio.to_thread(available_credit, token_id)
async def a_tier_base_limits():            return await asyncio.to_thread(tier_base_limits)
async def a_borrow_apr(tier, util_bps):    return await asyncio.to_thread(borrow_apr, tier, util_bps)
async def a_supply_apr(util_bps, rf=0):    return await asyncio.to_thread(supply_apr, util_bps, rf)
async def a_rate_curves():                 return await asyncio.to_thread(rate_curves)
async def a_pool_stats():                  return await asyncio.to_thread(pool_stats)
async def a_cgusdc_balance(wallet):        return await asyncio.to_thread(cgusdc_balance, wallet)
async def a_get_loan(loan_id):             return await asyncio.to_thread(get_loan, loan_id)
async def a_borrower_loan_ids(token_id):   return await asyncio.to_thread(borrower_loan_ids, token_id)
async def a_total_active_exposure(t_id):   return await asyncio.to_thread(total_active_exposure, t_id)
async def a_grace_period():                return await asyncio.to_thread(grace_period)
async def a_attestations_for(token_id):    return await asyncio.to_thread(attestations_for, token_id)
async def a_attestations_by(t_id):         return await asyncio.to_thread(attestations_by, t_id)
async def a_get_attestation(att_id):       return await asyncio.to_thread(get_attestation, att_id)
async def a_total_attestation_weight(tid): return await asyncio.to_thread(total_attestation_weight, tid)
async def a_insurance_state():             return await asyncio.to_thread(insurance_state)
async def a_treasury_state():              return await asyncio.to_thread(treasury_state)
async def a_agent_record(address):        return await asyncio.to_thread(agent_record, address)
async def a_usdc_balance(wallet):          return await asyncio.to_thread(usdc_balance, wallet)
async def a_identity_exists(token_id):     return await asyncio.to_thread(identity_exists, token_id)


def _u(units: int) -> float:
    return units / (10 ** USDC_DECIMALS)


# ----------------------------------------------------------------
# Identity & score
# ----------------------------------------------------------------

def token_id_of(wallet: str) -> int:
    try:
        return get("CreditIdentity").functions.tokenIdOf(Web3.to_checksum_address(wallet)).call()
    except Exception as e:
        log.warning(f"tokenIdOf({wallet}) failed: {e}")
        return 0


def identity_exists(token_id: int) -> bool:
    if token_id == 0:
        return False
    try:
        return get("CreditIdentity").functions.exists(token_id).call()
    except Exception:
        return False


def get_score(token_id: int) -> dict:
    """Returns { score, tier, is_stale, has_score }."""
    if token_id == 0:
        return {"score": 0, "tier": 1, "is_stale": True, "has_score": False}
    try:
        sr = get("ScoreRegistry")
        has = sr.functions.hasScore(token_id).call()
        if not has:
            return {"score": 0, "tier": 1, "is_stale": True, "has_score": False}
        value, tier, is_stale = sr.functions.getScore(token_id).call()
        return {
            "score": int(value),
            "tier": int(tier),
            "is_stale": bool(is_stale),
            "has_score": True,
        }
    except Exception as e:
        log.warning(f"getScore({token_id}) failed: {e}")
        return {"score": 0, "tier": 1, "is_stale": True, "has_score": False}


def get_pending_score(token_id: int) -> Optional[dict]:
    """Returns the pending submission or None if no submission has ever been made."""
    try:
        oracle = get("ScoringOracle")
        score, tier, reason_hash, submitted_at, finalized, challenged = \
            oracle.functions.pending(token_id).call()
        if submitted_at == 0:
            return None
        challenge_period = oracle.functions.challengePeriod().call()
        return {
            "score": int(score),
            "tier": int(tier),
            "reason_hash": "0x" + reason_hash.hex(),
            "submitted_at": int(submitted_at),
            "finalize_after": int(submitted_at + challenge_period),
            "finalized": bool(finalized),
            "challenged": bool(challenged),
        }
    except Exception as e:
        log.warning(f"pending({token_id}) failed: {e}")
        return None


def oracle_challenge_period() -> int:
    try:
        return int(get("ScoringOracle").functions.challengePeriod().call())
    except Exception:
        return 0


# ----------------------------------------------------------------
# Graduation
# ----------------------------------------------------------------

def graduation_state(token_id: int) -> dict:
    try:
        grad = get("RepaymentGraduation")
        tier = grad.functions.currentTier(token_id).call()
        streak = grad.functions.consecutiveOnTime(token_id).call()
        lifetime_on_time = grad.functions.lifetimeOnTime(token_id).call()
        lifetime_defaults = grad.functions.lifetimeDefaults(token_id).call()
        thresholds = [grad.functions.promotionThresholds(i).call() for i in range(5)]
        # Next-tier threshold
        next_threshold = None
        if tier < 5:
            next_threshold = int(thresholds[tier])  # tier index t means need thresholds[t] streak to reach t+1
        return {
            "tier": int(tier),
            "streak": int(streak),
            "lifetime_on_time": int(lifetime_on_time),
            "lifetime_defaults": int(lifetime_defaults),
            "thresholds": [int(t) for t in thresholds],
            "next_tier_threshold": next_threshold,
            "to_next_tier": max(0, next_threshold - streak) if next_threshold is not None else None,
        }
    except Exception as e:
        log.warning(f"graduation_state({token_id}) failed: {e}")
        return {
            "tier": 1, "streak": 0, "lifetime_on_time": 0, "lifetime_defaults": 0,
            "thresholds": [0, 2, 5, 12, 24], "next_tier_threshold": 2, "to_next_tier": 2,
        }


# ----------------------------------------------------------------
# Credit limit
# ----------------------------------------------------------------

def available_credit(token_id: int) -> dict:
    """Returns USDC-denominated values (floats)."""
    try:
        eng = get("CreditLimitEngine")
        limit, exposure, headroom = eng.functions.availableCredit(token_id).call()
        return {
            "limit_usdc": _u(limit),
            "exposure_usdc": _u(exposure),
            "headroom_usdc": _u(headroom),
            "limit_units": int(limit),
            "exposure_units": int(exposure),
            "headroom_units": int(headroom),
        }
    except Exception as e:
        log.warning(f"availableCredit({token_id}) failed: {e}")
        return {
            "limit_usdc": 0.0, "exposure_usdc": 0.0, "headroom_usdc": 0.0,
            "limit_units": 0, "exposure_units": 0, "headroom_units": 0,
        }


def tier_base_limits() -> list[float]:
    try:
        eng = get("CreditLimitEngine")
        return [_u(eng.functions.tierBaseLimit(i).call()) for i in range(5)]
    except Exception:
        return [20.0, 50.0, 150.0, 500.0, 2000.0]


# ----------------------------------------------------------------
# Interest rate model
# ----------------------------------------------------------------

def borrow_apr(tier: int, utilization_bps: int) -> int:
    try:
        return int(get("InterestRateModel").functions.borrowAPR(tier, utilization_bps).call())
    except Exception as e:
        log.warning(f"borrowAPR failed: {e}")
        return 0


def supply_apr(utilization_bps: int, reserve_factor_bps: int = 0) -> int:
    try:
        return int(get("InterestRateModel").functions.supplyAPR(utilization_bps, reserve_factor_bps).call())
    except Exception as e:
        log.warning(f"supplyAPR failed: {e}")
        return 0


def rate_curves() -> dict:
    try:
        irm = get("InterestRateModel")
        kink = int(irm.functions.kinkBps().call())
        curves = []
        for i in range(5):
            base, s1, s2 = irm.functions.curves(i).call()
            curves.append({"tier": i + 1, "base_bps": int(base), "slope1_bps": int(s1), "slope2_bps": int(s2)})
        return {"kink_bps": kink, "curves": curves}
    except Exception as e:
        log.warning(f"rate_curves failed: {e}")
        return {"kink_bps": 8000, "curves": []}


# ----------------------------------------------------------------
# Pool
# ----------------------------------------------------------------

def pool_stats() -> dict:
    try:
        pool = get("LendingPool")
        total_assets = pool.functions.totalAssets().call()
        total_borrowed = pool.functions.totalBorrowed().call()
        available = pool.functions.availableLiquidity().call()
        util_bps = pool.functions.utilizationRate().call()
        cum_interest = pool.functions.cumulativeInterest().call()
        cum_losses = pool.functions.cumulativeLosses().call()
        supply_cap = pool.functions.supplyCap().call()
        b_apr = borrow_apr(3, util_bps)  # tier-3 midpoint as a reference
        s_apr = supply_apr(util_bps, 0)
        return {
            "total_assets_usdc": _u(total_assets),
            "total_borrowed_usdc": _u(total_borrowed),
            "available_liquidity_usdc": _u(available),
            "utilization_bps": int(util_bps),
            "utilization_pct": float(util_bps) / 100.0,
            "cumulative_interest_usdc": _u(cum_interest),
            "cumulative_losses_usdc": _u(cum_losses),
            "supply_cap_usdc": _u(supply_cap),
            "reference_borrow_apr_bps": int(b_apr),
            "supply_apr_bps": int(s_apr),
        }
    except Exception as e:
        log.warning(f"pool_stats failed: {e}")
        return {}


def cgusdc_balance(wallet: str) -> dict:
    """Wallet's cgUSDC shares + their USDC equivalent."""
    try:
        pool = get("LendingPool")
        shares = pool.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        assets = pool.functions.convertToAssets(shares, Web3.to_checksum_address(wallet)).call() if shares > 0 else 0
        return {
            "shares": int(shares),
            "assets_usdc": _u(assets),
        }
    except Exception as e:
        log.warning(f"cgusdc_balance failed: {e}")
        return {"shares": 0, "assets_usdc": 0.0}


# ----------------------------------------------------------------
# Loans
# ----------------------------------------------------------------

LOAN_STATE_NAMES = ["None", "Active", "Repaid", "Late", "Defaulted"]


def get_loan(loan_id: int) -> Optional[dict]:
    try:
        loan = get("LoanManager").functions.getLoan(loan_id).call()
        token_id, principal, outstanding, interest_paid, originated_at, due_at, last_accrual, apr_bps, state = loan
        if state == 0:
            return None
        live_outstanding = get("LoanManager").functions.computeOutstanding(loan_id).call()
        return {
            "loan_id": int(loan_id),
            "token_id": int(token_id),
            "principal_usdc": _u(principal),
            "outstanding_principal_usdc": _u(outstanding),
            "outstanding_total_usdc": _u(live_outstanding),  # principal + accrued interest
            "interest_paid_usdc": _u(interest_paid),
            "originated_at": int(originated_at),
            "due_at": int(due_at),
            "last_accrual": int(last_accrual),
            "apr_bps": int(apr_bps),
            "state": LOAN_STATE_NAMES[state] if state < len(LOAN_STATE_NAMES) else "Unknown",
            "state_code": int(state),
        }
    except Exception as e:
        log.warning(f"get_loan({loan_id}) failed: {e}")
        return None


def borrower_loan_ids(token_id: int) -> list[int]:
    try:
        return [int(x) for x in get("LoanManager").functions.getBorrowerLoans(token_id).call()]
    except Exception as e:
        log.warning(f"getBorrowerLoans({token_id}) failed: {e}")
        return []


def total_active_exposure(token_id: int) -> float:
    try:
        return _u(get("LoanManager").functions.totalActiveExposure(token_id).call())
    except Exception:
        return 0.0


def grace_period() -> int:
    try:
        return int(get("LoanManager").functions.gracePeriod().call())
    except Exception:
        return 7 * 86400


# ----------------------------------------------------------------
# Attestations
# ----------------------------------------------------------------

def attestations_for(token_id: int) -> list[dict]:
    try:
        rows = get("SocialAttestation").functions.attestationsFor(token_id).call()
        out = []
        for r in rows:
            attester_token_id, subject_token_id, bond, created_at, expires_at, active, rel_type = r
            out.append({
                "attester_token_id": int(attester_token_id),
                "subject_token_id": int(subject_token_id),
                "bond_usdc": _u(bond),
                "created_at": int(created_at),
                "expires_at": int(expires_at),
                "active": bool(active),
                "relationship_type": "0x" + rel_type.hex(),
            })
        return out
    except Exception as e:
        log.warning(f"attestationsFor({token_id}) failed: {e}")
        return []


def attestations_by(attester_token_id: int) -> list[int]:
    try:
        return [int(x) for x in get("SocialAttestation").functions.attestationsByAttester(attester_token_id).call()]
    except Exception:
        return []


def get_attestation(attestation_id: int) -> Optional[dict]:
    try:
        r = get("SocialAttestation").functions.getAttestation(attestation_id).call()
        attester_token_id, subject_token_id, bond, created_at, expires_at, active, rel_type = r
        if created_at == 0:
            return None
        unlock_at = int(get("SocialAttestation").functions.revokeUnlockAt(attestation_id).call())
        return {
            "attestation_id": int(attestation_id),
            "attester_token_id": int(attester_token_id),
            "subject_token_id": int(subject_token_id),
            "bond_usdc": _u(bond),
            "created_at": int(created_at),
            "expires_at": int(expires_at),
            "active": bool(active),
            "relationship_type": "0x" + rel_type.hex(),
            "revoke_unlock_at": unlock_at,
        }
    except Exception as e:
        log.warning(f"getAttestation({attestation_id}) failed: {e}")
        return None


def total_attestation_weight(token_id: int) -> float:
    try:
        return _u(get("SocialAttestation").functions.totalWeight(token_id).call())
    except Exception:
        return 0.0


# ----------------------------------------------------------------
# Insurance, treasury, agents
# ----------------------------------------------------------------

def insurance_state() -> dict:
    try:
        f = get("InsuranceFund")
        return {
            "balance_usdc": _u(f.functions.balance().call()),
            "total_covered_usdc": _u(f.functions.totalCovered().call()),
            "recipient": f.functions.coverageRecipient().call(),
        }
    except Exception as e:
        log.warning(f"insurance_state failed: {e}")
        return {"balance_usdc": 0.0, "total_covered_usdc": 0.0, "recipient": ""}


def treasury_state() -> dict:
    try:
        t = get("Treasury")
        return {
            "insurance_bps": int(t.functions.insuranceBps().call()),
            "operations_bps": int(t.functions.operationsBps().call()),
            "agent_rewards_bps": int(t.functions.agentRewardsBps().call()),
            "insurance_fund": t.functions.insuranceFund().call(),
            "operations": t.functions.operations().call(),
            "agent_rewards": t.functions.agentRewards().call(),
        }
    except Exception as e:
        log.warning(f"treasury_state failed: {e}")
        return {}


def agent_record(address: str) -> dict:
    try:
        r = get("AgentRegistry").functions.agents(Web3.to_checksum_address(address)).call()
        role, stake, reputation, registered_at, unbonding_at, active = r
        role_names = ["None", "DataCollector", "Underwriter", "PoolManager", "Recovery"]
        return {
            "address": Web3.to_checksum_address(address),
            "role_code": int(role),
            "role": role_names[role] if role < len(role_names) else "Unknown",
            "stake_usdc": _u(stake),
            "reputation": int(reputation),
            "registered_at": int(registered_at),
            "unbonding_at": int(unbonding_at),
            "active": bool(active),
        }
    except Exception as e:
        log.warning(f"agent_record failed: {e}")
        return {}


# ----------------------------------------------------------------
# USDC
# ----------------------------------------------------------------

def usdc_balance(wallet: str) -> float:
    try:
        bal = get("USDC").functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        return _u(bal)
    except Exception:
        return 0.0