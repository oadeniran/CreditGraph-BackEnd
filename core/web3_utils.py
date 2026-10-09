"""
Receipt + event utilities. Chain-aware.
"""

import logging
from typing import Optional, Any

from core.contracts import get_w3, get_contracts

log = logging.getLogger("creditgraph.web3_utils")


def get_receipt(chain_key: str, tx_hash: str, timeout: int = 30) -> Optional[Any]:
    if not tx_hash or not tx_hash.startswith("0x"):
        return None
    try:
        w3 = get_w3(chain_key)
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception as e:
        log.warning(f"[{chain_key}] receipt fetch failed for {tx_hash}: {e}")
        return None


def verify_tx_succeeded(chain_key: str, tx_hash: str, timeout: int = 30) -> bool:
    receipt = get_receipt(chain_key, tx_hash, timeout=timeout)
    if receipt is None:
        return False
    return receipt.status == 1


def find_event(chain_key: str, contract_name: str, event_name: str, receipt) -> Optional[dict]:
    from web3.logs import DISCARD
    contracts = get_contracts(chain_key)
    contract = contracts.get(contract_name)
    if contract is None:
        return None
    try:
        event_obj = getattr(contract.events, event_name)
        logs = event_obj().process_receipt(receipt, errors=DISCARD)
        for ev in logs:
            args = ev.get("args")
            if args is not None:
                return dict(args)
    except Exception as e:
        log.warning(f"[{chain_key}] find_event {contract_name}.{event_name}: {e}")
    return None


def find_all_events(chain_key: str, contract_name: str, event_name: str, receipt) -> list[dict]:
    from web3.logs import DISCARD
    contracts = get_contracts(chain_key)
    contract = contracts.get(contract_name)
    if contract is None:
        return []
    out = []
    try:
        event_obj = getattr(contract.events, event_name)
        logs = event_obj().process_receipt(receipt, errors=DISCARD)
        for ev in logs:
            args = ev.get("args")
            if args is not None:
                out.append(dict(args))
    except Exception as e:
        log.warning(f"[{chain_key}] find_all_events {contract_name}.{event_name}: {e}")
    return out