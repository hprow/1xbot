import os
import asyncio
import tg_utils
import orjson
from datetime import datetime, timezone
from web3 import Web3
from dotenv import load_dotenv

# Polymarket SDKs
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_builder_relayer_client.client import RelayClient
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
from py_builder_relayer_client.models import SafeTransaction, OperationType

load_dotenv()

# Constants
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_ABI = orjson.loads(
    b'[{"constant":true,"inputs":[{"name":"","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"","type":"bytes32"},{"name":"","type":"uint256"}],"name":"payoutNumerators","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"","type":"address"},{"name":"","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')
REDEEM_ABI = [{"name": "redeemPositions", "type": "function", "inputs": [{"name": "collateralToken", "type": "address"},
                                                                         {"name": "parentCollectionId",
                                                                          "type": "bytes32"},
                                                                         {"name": "conditionId", "type": "bytes32"},
                                                                         {"name": "indexSets", "type": "uint256[]"}]}]

_client = None
_cached_fee_rate = None


def get_client() -> ClobClient:
    global _client
    if _client is not None: return _client

    pk = os.getenv("POLY_PRIVATE_KEY")
    funder = os.getenv("POLY_FUNDER_ADDRESS")

    if not pk or not funder:
        raise ValueError("Missing POLY_PRIVATE_KEY or POLY_FUNDER_ADDRESS in .env")

    _client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        signature_type=2,
        funder=funder
    )
    print(f"[*] Authenticating Proxy Vault: {funder[:10]}...")
    creds = _client.create_or_derive_api_creds()
    _client.set_api_creds(creds)
    return _client


# --- Core Trading Functions ---

async def buy(market_token_id: str, price: float, size: float):
    client = get_client()
    order_args = OrderArgs(price=price, size=size, side=BUY, token_id=market_token_id)
    try:
        signed_order = client.create_order(order_args)
        resp = await asyncio.to_thread(client.post_order, signed_order, OrderType.FOK)
        print(f"[+] BUY Call Completed. Resp: {resp}")
        return resp
    except Exception as e:
        print(f"[-] BUY Call Exception: {e}")
        return None


async def sell(market_token_id: str, price: float, size: float):
    client = get_client()
    order_args = OrderArgs(price=0.01, size=size, side=SELL, token_id=market_token_id)
    try:
        signed_order = client.create_order(order_args)
        resp = await asyncio.to_thread(client.post_order, signed_order, OrderType.FOK)
        return resp
    except Exception as e:
        print(f"[-] SELL Call Exception: {e}")
        return None


# --- Verification Logic ---

async def get_token_balance(token_id: str) -> float:
    """Reads the exact amount of conditional tokens currently sitting in the proxy vault."""
    try:
        w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
        funder = os.getenv("POLY_FUNDER_ADDRESS")
        ctf_contract = w3.eth.contract(address=w3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

        balance = await asyncio.to_thread(
            ctf_contract.functions.balanceOf(w3.to_checksum_address(funder), int(token_id)).call
        )
        return float(balance) / 1e6  # Polymarket Tokens natively use 6 decimals
    except Exception as e:
        print(f"[-] Error querying chain for token balance: {e}")
        return 0.0


# --- Batched Cashout Logic ---

async def process_batch_cashouts(pending_trades: list) -> list:
    """
    Evaluates unresolved trades. Discards losses, constructs SafeTransaction payloads for wins,
    and executes them all simultaneously to consume exactly ONE Relayer quota interaction.
    Returns the list of trades that are STILL awaiting resolution.
    """
    if not pending_trades:
        return []

    w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
    funder = os.getenv("POLY_FUNDER_ADDRESS")
    pk = os.getenv("POLY_PRIVATE_KEY")

    ctf_contract = w3.eth.contract(address=w3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

    still_pending = []
    tx_batch = []
    won_trades_info = []

    now_ts = datetime.now(timezone.utc).timestamp()

    for trade in pending_trades:
        cond_id = trade["condition_id"]

        # 1. Skip if the event hasn't expired yet
        if now_ts < trade["expiry_ts"]:
            still_pending.append(trade)
            continue

        cond_bytes = w3.to_bytes(hexstr=cond_id)

        # 2. Check if Oracle has resolved the market
        denom = await asyncio.to_thread(ctf_contract.functions.payoutDenominator(cond_bytes).call)
        if denom == 0:
            # Oracle has not populated the result yet
            still_pending.append(trade)
            continue

        # 3. Assess Result
        index = 0 if trade["side_name"] == "UP" else 1
        payout_num = await asyncio.to_thread(ctf_contract.functions.payoutNumerators(cond_bytes, index).call)

        if payout_num == 0:
            # 📉 LOST
            tg_utils.update_trade_pnl(cond_id, -trade["spend"])
            tg_utils.send_tg_msg(
                f"❌ <b>MARKET RESOLVED: LOST</b>\nTokens for {cond_id[:8]}... expired worthless.\nPnL: <b>-${trade['spend']:.2f} USDC</b>")
            continue

        # 📈 WON - Calculate payout
        balance = await asyncio.to_thread(
            ctf_contract.functions.balanceOf(w3.to_checksum_address(funder), int(trade["token_id"])).call
        )

        if balance == 0:
            continue  # Already redeemed or manually dumped

        payout_usdc = balance / 1e6
        pnl = payout_usdc - trade["spend"]

        # 4. Construct the SafeTransaction for this winning condition
        call_data = w3.eth.contract(abi=REDEEM_ABI).encode_abi(
            "redeemPositions",
            [w3.to_checksum_address(USDC_E_ADDRESS), b'\x00' * 32, cond_bytes, [1, 2]]
        )

        tx = SafeTransaction(
            to=w3.to_checksum_address(CTF_ADDRESS),
            operation=OperationType.Call,
            data=call_data,
            value="0"
        )
        tx_batch.append(tx)
        won_trades_info.append({"trade": trade, "payout_usdc": payout_usdc, "pnl": pnl})

    # 5. Execute all SafeTransactions in one massive Relayer Call
    if tx_batch:
        print(f"[*] [Batch Cashout] Executing gasless redemption for {len(tx_batch)} winning markets...")

        creds = BuilderApiKeyCreds(
            key=os.getenv("POLY_BUILDER_API_KEY"),
            secret=os.getenv("POLY_BUILDER_SECRET"),
            passphrase=os.getenv("POLY_BUILDER_PASSPHRASE")
        )
        relayer = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com/",
            chain_id=137,
            private_key=pk,
            builder_config=BuilderConfig(local_builder_creds=creds)
        )

        try:
            # Passing a list of transactions executes them all under ONE block operation/quota limit
            response = await asyncio.to_thread(relayer.execute, tx_batch, metadata="Batch Redeem Winnings")

            if response and hasattr(response, 'transaction_hash'):
                print(f"\n[$$$] BATCH CASHOUT SUCCESS! Hash: {response.transaction_hash}")
                for item in won_trades_info:
                    tr = item["trade"]
                    tg_utils.update_trade_pnl(tr["condition_id"], item["pnl"])
                    tg_utils.send_tg_msg(
                        f"✅ <b>MARKET RESOLVED: WON!</b>\nPayout: <b>${item['payout_usdc']:.2f} USDC</b>\nPnL: <b>+${item['pnl']:.2f} USDC</b>")
            else:
                print(f"[-] Batch cashout unexpected response: {response}")
                # Keep them in pending to retry next cycle
                still_pending.extend([item["trade"] for item in won_trades_info])

        except Exception as e:
            print(f"[!] Batch Relayer execution failed: {e}")
            # Keep them in pending if the Relayer threw a 429/quota error
            still_pending.extend([item["trade"] for item in won_trades_info])

    return still_pending