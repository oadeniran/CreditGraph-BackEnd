"""
Read-only chain queries. Chain-aware: every function takes `chain_key`.
Includes async wrappers so routes can parallelize with asyncio.gather.
"""

import asyncio
import logging
from typing import Optional
from web3 import Web3

from core.contracts import get_contracts, get_contract, get_w3

log = logging.getLogger("creditgraph.chain_reader")

USDC_DECIMALS = 6


def _u(units: int) -> float:
    return units / (10 ** USDC_DECIMALS)


# ----------------------------------------------------------------
# Identity & score
# ----------------------------------------------------------------

def token_id_of(chain_key: str, wallet: str) -> int:
    try:
        return get_contract(chain_key, "CreditIdentity").functions.tokenIdOf(
            Web3.to_checksum_address(wallet)
        ).call()
    except Exception as e:
        log.warning(f"[{chain_key}] tokenIdOf({wallet}) failed: {e}")
        return 0


def identity_exists(chain_key: str, token_id: int) -> bool:
    if token_id == 0:
        return False
    try:
        return get_contract(chain_key, "CreditIdentity").functions.exists(token_id).call()
    except Exception:
        return False


def get_score(chain_key: str, token_id: int) -> dict:
    if token_id == 0:
        return {"score": 0, "tier": 1, "is_stale": True, "has_score": False}
    try:
        sr = get_contract(chain_key, "ScoreRegistry")
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
        log.warning(f"[{chain_key}] getScore({token_id}) failed: {e}")
        return {"score": 0, "tier": 1, "is_stale": True, "has_score": False}


def get_pending_score(chain_key: str, token_id: int) -> Optional[dict]:
    try:
        oracle = get_contract(chain_key, "ScoringOracle")
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
        log.warning(f"[{chain_key}] pending({token_id}) failed: {e}")
        return None


def oracle_challenge_period(chain_key: str) -> int:
    try:
        return int(get_contract(chain_key, "ScoringOracle").functions.challengePeriod().call())
    except Exception:
        return 0


# ----------------------------------------------------------------
# Graduation
# ----------------------------------------------------------------

def graduation_state(chain_key: str, token_id: int) -> dict:
    try:
        grad = get_contract(chain_key, "RepaymentGraduation")
        tier = grad.functions.currentTier(token_id).call()
        streak = grad.functions.consecutiveOnTime(token_id).call()
        lifetime_on_time = grad.functions.lifetimeOnTime(token_id).call()
        lifetime_defaults = grad.functions.lifetimeDefaults(token_id).call()
        thresholds = [grad.functions.promotionThresholds(i).call() for i in range(5)]
        next_threshold = int(thresholds[tier]) if tier < 5 else None
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
        log.warning(f"[{chain_key}] graduation_state({token_id}) failed: {e}")
        return {
            "tier": 1, "streak": 0, "lifetime_on_time": 0, "lifetime_defaults": 0,
            "thresholds": [0, 2, 5, 12, 24], "next_tier_threshold": 2, "to_next_tier": 2,
        }


# ----------------------------------------------------------------
# Credit limit
# ----------------------------------------------------------------

def available_credit(chain_key: str, token_id: int) -> dict:
    try:
        eng = get_contract(chain_key, "CreditLimitEngine")
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
        log.warning(f"[{chain_key}] availableCredit({token_id}) failed: {e}")
        return {"limit_usdc": 0.0, "exposure_usdc": 0.0, "headroom_usdc": 0.0,
                "limit_units": 0, "exposure_units": 0, "headroom_units": 0}


def tier_base_limits(chain_key: str) -> list[float]:
    try:
        eng = get_contract(chain_key, "CreditLimitEngine")
        return [_u(eng.functions.tierBaseLimit(i).call()) for i in range(5)]
    except Exception:
        return [20.0, 50.0, 150.0, 500.0, 2000.0]


# ----------------------------------------------------------------
# Interest rate model
# ----------------------------------------------------------------

def borrow_apr(chain_key: str, tier: int, utilization_bps: int) -> int:
    try:
        return int(get_contract(chain_key, "InterestRateModel").functions.borrowAPR(tier, utilization_bps).call())
    except Exception as e:
        log.warning(f"[{chain_key}] borrowAPR failed: {e}")
        return 0


def supply_apr(chain_key: str, utilization_bps: int, reserve_factor_bps: int = 0) -> int:
    try:
        return int(get_contract(chain_key, "InterestRateModel").functions.supplyAPR(utilization_bps, reserve_factor_bps).call())
    except Exception as e:
        log.warning(f"[{chain_key}] supplyAPR failed: {e}")
        return 0


def rate_curves(chain_key: str) -> dict:
    try:
        irm = get_contract(chain_key, "InterestRateModel")
        kink = int(irm.functions.kinkBps().call())
        curves = []
        for i in range(5):
            base, s1, s2 = irm.functions.curves(i).call()
            curves.append({"tier": i + 1, "base_bps": int(base), "slope1_bps": int(s1), "slope2_bps": int(s2)})
        return {"kink_bps": kink, "curves": curves}
    except Exception as e:
        log.warning(f"[{chain_key}] rate_curves failed: {e}")
        return {"kink_bps": 8000, "curves": []}


# ----------------------------------------------------------------
# Pool
# ----------------------------------------------------------------

def pool_stats(chain_key: str) -> dict:
    try:
        pool = get_contract(chain_key, "LendingPool")
        total_assets = pool.functions.totalAssets().call()
        total_borrowed = pool.functions.totalBorrowed().call()
        available = pool.functions.availableLiquidity().call()
        util_bps = pool.functions.utilizationRate().call()
        cum_interest = pool.functions.cumulativeInterest().call()
        cum_losses = pool.functions.cumulativeLosses().call()
        supply_cap = pool.functions.supplyCap().call()
        b_apr = borrow_apr(chain_key, 3, util_bps)
        s_apr = supply_apr(chain_key, util_bps, 0)
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
        log.warning(f"[{chain_key}] pool_stats failed: {e}")
        return {}


def cgusdc_balance(chain_key: str, wallet: str) -> dict:
    try:
        pool = get_contract(chain_key, "LendingPool")
        shares = pool.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        assets = pool.functions.convertToAssets(shares, Web3.to_checksum_address(wallet)).call() if shares > 0 else 0
        return {"shares": int(shares), "assets_usdc": _u(assets)}
    except Exception as e:
        log.warning(f"[{chain_key}] cgusdc_balance failed: {e}")
        return {"shares": 0, "assets_usdc": 0.0}


# ----------------------------------------------------------------
# Loans
# ----------------------------------------------------------------

LOAN_STATE_NAMES = ["None", "Active", "Repaid", "Late", "Defaulted"]


def get_loan(chain_key: str, loan_id: int) -> Optional[dict]:
    try:
        lm = get_contract(chain_key, "LoanManager")
        loan = lm.functions.getLoan(loan_id).call()
        token_id, principal, outstanding, interest_paid, originated_at, due_at, last_accrual, apr_bps, state = loan
        if state == 0:
            return None
        live_outstanding = lm.functions.computeOutstanding(loan_id).call()
        return {
            "loan_id": int(loan_id),
            "token_id": int(token_id),
            "principal_usdc": _u(principal),
            "outstanding_principal_usdc": _u(outstanding),
            "outstanding_total_usdc": _u(live_outstanding),
            "interest_paid_usdc": _u(interest_paid),
            "originated_at": int(originated_at),
            "due_at": int(due_at),
            "last_accrual": int(last_accrual),
            "apr_bps": int(apr_bps),
            "state": LOAN_STATE_NAMES[state] if state < len(LOAN_STATE_NAMES) else "Unknown",
            "state_code": int(state),
        }
    except Exception as e:
        log.warning(f"[{chain_key}] get_loan({loan_id}) failed: {e}")
        return None


def borrower_loan_ids(chain_key: str, token_id: int) -> list[int]:
    try:
        return [int(x) for x in get_contract(chain_key, "LoanManager").functions.getBorrowerLoans(token_id).call()]
    except Exception as e:
        log.warning(f"[{chain_key}] getBorrowerLoans({token_id}) failed: {e}")
        return []


def total_active_exposure(chain_key: str, token_id: int) -> float:
    try:
        return _u(get_contract(chain_key, "LoanManager").functions.totalActiveExposure(token_id).call())
    except Exception:
        return 0.0


def grace_period(chain_key: str) -> int:
    try:
        return int(get_contract(chain_key, "LoanManager").functions.gracePeriod().call())
    except Exception:
        return 7 * 86400


# ----------------------------------------------------------------
# Attestations
# ----------------------------------------------------------------

def attestations_for(chain_key: str, token_id: int) -> list[dict]:
    try:
        rows = get_contract(chain_key, "SocialAttestation").functions.attestationsFor(token_id).call()
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
        log.warning(f"[{chain_key}] attestationsFor({token_id}) failed: {e}")
        return []


def attestations_by(chain_key: str, attester_token_id: int) -> list[int]:
    try:
        return [int(x) for x in get_contract(chain_key, "SocialAttestation").functions.attestationsByAttester(attester_token_id).call()]
    except Exception:
        return []


def get_attestation(chain_key: str, attestation_id: int) -> Optional[dict]:
    try:
        sa = get_contract(chain_key, "SocialAttestation")
        r = sa.functions.getAttestation(attestation_id).call()
        attester_token_id, subject_token_id, bond, created_at, expires_at, active, rel_type = r
        if created_at == 0:
            return None
        unlock_at = int(sa.functions.revokeUnlockAt(attestation_id).call())
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
        log.warning(f"[{chain_key}] getAttestation({attestation_id}) failed: {e}")
        return None


def total_attestation_weight(chain_key: str, token_id: int) -> float:
    try:
        return _u(get_contract(chain_key, "SocialAttestation").functions.totalWeight(token_id).call())
    except Exception:
        return 0.0


# ----------------------------------------------------------------
# Insurance, treasury, agents
# ----------------------------------------------------------------

def insurance_state(chain_key: str) -> dict:
    try:
        f = get_contract(chain_key, "InsuranceFund")
        return {
            "balance_usdc": _u(f.functions.balance().call()),
            "total_covered_usdc": _u(f.functions.totalCovered().call()),
            "recipient": f.functions.coverageRecipient().call(),
        }
    except Exception as e:
        log.warning(f"[{chain_key}] insurance_state failed: {e}")
        return {"balance_usdc": 0.0, "total_covered_usdc": 0.0, "recipient": ""}


def treasury_state(chain_key: str) -> dict:
    try:
        t = get_contract(chain_key, "Treasury")
        return {
            "insurance_bps": int(t.functions.insuranceBps().call()),
            "operations_bps": int(t.functions.operationsBps().call()),
            "agent_rewards_bps": int(t.functions.agentRewardsBps().call()),
            "insurance_fund": t.functions.insuranceFund().call(),
            "operations": t.functions.operations().call(),
            "agent_rewards": t.functions.agentRewards().call(),
        }
    except Exception as e:
        log.warning(f"[{chain_key}] treasury_state failed: {e}")
        return {}


def agent_record(chain_key: str, address: str) -> dict:
    try:
        r = get_contract(chain_key, "AgentRegistry").functions.agents(Web3.to_checksum_address(address)).call()
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
        log.warning(f"[{chain_key}] agent_record failed: {e}")
        return {}


# ----------------------------------------------------------------
# USDC
# ----------------------------------------------------------------

def usdc_balance(chain_key: str, wallet: str) -> float:
    try:
        bal = get_contract(chain_key, "USDC").functions.balanceOf(Web3.to_checksum_address(wallet)).call()
        return _u(bal)
    except Exception:
        return 0.0


# ================================================================
# ASYNC WRAPPERS — use these from async routes to avoid blocking
# ================================================================

async def a_token_id_of(chain_key, wallet):              return await asyncio.to_thread(token_id_of, chain_key, wallet)
async def a_identity_exists(chain_key, token_id):        return await asyncio.to_thread(identity_exists, chain_key, token_id)
async def a_get_score(chain_key, token_id):              return await asyncio.to_thread(get_score, chain_key, token_id)
async def a_get_pending_score(chain_key, token_id):      return await asyncio.to_thread(get_pending_score, chain_key, token_id)
async def a_oracle_challenge_period(chain_key):          return await asyncio.to_thread(oracle_challenge_period, chain_key)
async def a_graduation_state(chain_key, token_id):       return await asyncio.to_thread(graduation_state, chain_key, token_id)
async def a_available_credit(chain_key, token_id):       return await asyncio.to_thread(available_credit, chain_key, token_id)
async def a_tier_base_limits(chain_key):                 return await asyncio.to_thread(tier_base_limits, chain_key)
async def a_borrow_apr(chain_key, tier, util_bps):       return await asyncio.to_thread(borrow_apr, chain_key, tier, util_bps)
async def a_supply_apr(chain_key, util_bps, rf=0):       return await asyncio.to_thread(supply_apr, chain_key, util_bps, rf)
async def a_rate_curves(chain_key):                      return await asyncio.to_thread(rate_curves, chain_key)
async def a_pool_stats(chain_key):                       return await asyncio.to_thread(pool_stats, chain_key)
async def a_cgusdc_balance(chain_key, wallet):           return await asyncio.to_thread(cgusdc_balance, chain_key, wallet)
async def a_get_loan(chain_key, loan_id):                return await asyncio.to_thread(get_loan, chain_key, loan_id)
async def a_borrower_loan_ids(chain_key, token_id):      return await asyncio.to_thread(borrower_loan_ids, chain_key, token_id)
async def a_total_active_exposure(chain_key, token_id):  return await asyncio.to_thread(total_active_exposure, chain_key, token_id)
async def a_grace_period(chain_key):                     return await asyncio.to_thread(grace_period, chain_key)
async def a_attestations_for(chain_key, token_id):       return await asyncio.to_thread(attestations_for, chain_key, token_id)
async def a_attestations_by(chain_key, token_id):        return await asyncio.to_thread(attestations_by, chain_key, token_id)
async def a_get_attestation(chain_key, att_id):          return await asyncio.to_thread(get_attestation, chain_key, att_id)
async def a_total_attestation_weight(chain_key, tid):    return await asyncio.to_thread(total_attestation_weight, chain_key, tid)
async def a_insurance_state(chain_key):                  return await asyncio.to_thread(insurance_state, chain_key)
async def a_treasury_state(chain_key):                   return await asyncio.to_thread(treasury_state, chain_key)
async def a_agent_record(chain_key, address):            return await asyncio.to_thread(agent_record, chain_key, address)
async def a_usdc_balance(chain_key, wallet):             return await asyncio.to_thread(usdc_balance, chain_key, wallet)