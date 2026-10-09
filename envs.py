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


# --- Database ---
MONGO_URI = _get("MONGO_URI", "mongodb://localhost:27017")

# --- Signer keys (shared across all chains) ---
DEPLOYER_PRIVATE_KEY = _get("DEPLOYER_PRIVATE_KEY")
ADMIN_SEED = _get("ADMIN_SEED", "creditgraph-admin-v1")
AGENT_SEEDS = [
    _get("AGENT_SEED_1", "creditgraph-agent-1-v1"),
    _get("AGENT_SEED_2", "creditgraph-agent-2-v1"),
    _get("AGENT_SEED_3", "creditgraph-agent-3-v1"),
]
AGENT_STAKE_USDC = float(_get("AGENT_STAKE_USDC", "10"))

# --- Indexer ---
INDEXER_POLL_SECONDS = int(_get("INDEXER_POLL_SECONDS", "15"))
INDEXER_ENABLED = _bool("INDEXER_ENABLED", True)

# --- Behavior ---
TRY_SET_CHALLENGE_PERIOD_ZERO = _bool("TRY_SET_CHALLENGE_PERIOD_ZERO", True)