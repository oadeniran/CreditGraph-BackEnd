from dotenv import load_dotenv
import os

load_dotenv()

def _get(key, default=None):
    v = os.getenv(key)
    return v if v is not None and v != "" else default

def _bool(key, default=False):
    v = os.getenv(key, "").lower()
    if v in ("1", "true", "yes", "y"):
        return True
    if v in ("0", "false", "no", "n"):
        return False
    return default

# --- Database / RPC ---
MONGO_URI = _get("MONGO_URI", "mongodb://localhost:27017")
RPC_URL = _get("RPC_URL", "https://sepolia-rollup.arbitrum.io/rpc")
CHAIN_ID = int(_get("CHAIN_ID", "421614"))

# --- Keys ---
DEPLOYER_PRIVATE_KEY = _get("DEPLOYER_PRIVATE_KEY")
ADMIN_SEED = _get("ADMIN_SEED", "creditgraph-admin-v1")
AGENT_SEEDS = [
    _get("AGENT_SEED_1", "creditgraph-agent-1-v1"),
    _get("AGENT_SEED_2", "creditgraph-agent-2-v1"),
    _get("AGENT_SEED_3", "creditgraph-agent-3-v1"),
]
AGENT_STAKE_USDC = float(_get("AGENT_STAKE_USDC", "10"))

# --- Addresses ---
ADDR = {
    "USDC": _get("USDC_ADDRESS"),
    "AccessController": _get("ACCESS_CONTROLLER_ADDRESS"),
    "CreditIdentity": _get("CREDIT_IDENTITY_ADDRESS"),
    "ScoreRegistry": _get("SCORE_REGISTRY_ADDRESS"),
    "ScoringOracle": _get("SCORING_ORACLE_ADDRESS"),
    "ZKAttestationVerifier": _get("ZK_VERIFIER_ADDRESS"),
    "DataOracleAdapter": _get("DATA_ORACLE_ADAPTER_ADDRESS"),
    "AgentRegistry": _get("AGENT_REGISTRY_ADDRESS"),
    "X402PaymentRouter": _get("X402_PAYMENT_ROUTER_ADDRESS"),
    "RepaymentGraduation": _get("REPAYMENT_GRADUATION_ADDRESS"),
    "SocialAttestation": _get("SOCIAL_ATTESTATION_ADDRESS"),
    "InterestRateModel": _get("INTEREST_RATE_MODEL_ADDRESS"),
    "CreditLimitEngine": _get("CREDIT_LIMIT_ENGINE_ADDRESS"),
    "LendingPool": _get("LENDING_POOL_ADDRESS"),
    "LoanManager": _get("LOAN_MANAGER_ADDRESS"),
    "InsuranceFund": _get("INSURANCE_FUND_ADDRESS"),
    "CreditSlasher": _get("CREDIT_SLASHER_ADDRESS"),
    "Treasury": _get("TREASURY_ADDRESS"),
}

# --- Indexer ---
INDEXER_START_BLOCK = int(_get("INDEXER_START_BLOCK", "0"))
INDEXER_POLL_SECONDS = int(_get("INDEXER_POLL_SECONDS", "15"))
INDEXER_ENABLED = _bool("INDEXER_ENABLED", True)

# --- Behavior ---
TRY_SET_CHALLENGE_PERIOD_ZERO = _bool("TRY_SET_CHALLENGE_PERIOD_ZERO", True)