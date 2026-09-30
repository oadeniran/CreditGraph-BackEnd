"""
Receipt + event utilities for verifying FE-submitted transactions.
No more 'fallback to True'. If we can't verify, we return a clear failure.
"""

import logging
from typing import Optional, Any

from web3 import Web3

from core.contracts import w3, contracts

log = logging.getLogger("creditgraph.web3_utils")


def get_receipt(tx_hash: str, timeout: int = 30) -> Optional[Any]:
    """Fetch a transaction receipt. Returns None if not found / pending."""
    if not tx_hash or not tx_hash.startswith("0x"):
        return None
    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception as e:
        log.warning(f"Receipt fetch failed for {tx_hash}: {e}")
        return None


def verify_tx_succeeded(tx_hash: str, timeout: int = 30) -> bool:
    """Returns True iff the tx is mined AND status==1."""
    receipt = get_receipt(tx_hash, timeout=timeout)
    if receipt is None:
        return False
    return receipt.status == 1


def find_event(contract_name: str, event_name: str, receipt) -> Optional[dict]:
    """Find the first matching event in a receipt and return its args as a dict."""
    contract = contracts.get(contract_name)
    if contract is None:
        return None
    try:
        event_obj = getattr(contract.events, event_name)
        logs = event_obj().process_receipt(receipt, errors="ignore")
        for ev in logs:
            args = ev.get("args")
            if args is not None:
                return dict(args)
    except Exception as e:
        log.warning(f"find_event {contract_name}.{event_name}: {e}")
    return None


def find_all_events(contract_name: str, event_name: str, receipt) -> list[dict]:
    """All matching events as a list of arg dicts."""
    contract = contracts.get(contract_name)
    if contract is None:
        return []
    out = []
    try:
        event_obj = getattr(contract.events, event_name)
        logs = event_obj().process_receipt(receipt, errors="ignore")
        for ev in logs:
            args = ev.get("args")
            if args is not None:
                out.append(dict(args))
    except Exception as e:
        log.warning(f"find_all_events {contract_name}.{event_name}: {e}")
    return out