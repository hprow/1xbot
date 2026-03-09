import os
import asyncio
import tg_utils
import orjson
import re
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

_client = None


def get_client() -> ClobClient:
    global _client
    if _client is not None:
        return _client

    pk = os.getenv("POLY_PRIVATE_KEY")
    # This must be the "Proxy" address you just found in the UI
    funder = os.getenv("POLY_FUNDER_ADDRESS")

    if not pk or not funder:
        raise ValueError("Missing POLY_PRIVATE_KEY or POLY_FUNDER_ADDRESS in .env")

    # 1. Initialize for Gnosis Safe (MetaMask + Proxy)
    _client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,  # Your MetaMask Private Key
        signature_type=2,  # 2 = Gnosis Safe / Proxy
        funder=funder  # Your Vault Address
    )

    # 2. Derive API credentials
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
        print(f"[+] BUY Success: {resp}")
        return resp
    except Exception as e:
        print(f"[-] BUY Failed: {e}")
        return None


async def sell(market_token_id: str, price: float, size: float):
    client = get_client()
    order_args = OrderArgs(price=0.01, size=size, side=SELL, token_id=market_token_id)

    try:
        signed_order = client.create_order(order_args)
        resp = await asyncio.to_thread(client.post_order, signed_order, OrderType.FOK)
        print(f"[+] SELL Success: {resp}")
        return resp
    except Exception as e:
        print(f"[-] SELL Failed: {e}")
        return None


# --- Cashout Logic ---

async def _cashout_task(condition_id: str, bought_token_id: str, end_date_iso: str, side_name: str, spend_usdt: float):
    """
    Monitors market resolution and executes a gasless redemption via the Relayer.
    """
    w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
    funder = os.getenv("POLY_FUNDER_ADDRESS")
    pk = os.getenv("POLY_PRIVATE_KEY")

    # 1. Wait for Expiration
    try:
        end_time = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        print(f"[*] [Cashout] Monitoring market until {end_time.strftime('%H:%M:%S')}...")
        while datetime.now(timezone.utc) < end_time:
            await asyncio.sleep(30)
        await asyncio.sleep(20)  # Buffer for block confirmation
    except Exception as e:
        print(f"[!] Timer error: {e}")

    # 2. Wait for Oracle Settlement (payoutDenominator > 0)
    ctf_contract = w3.eth.contract(
        address=w3.to_checksum_address(CTF_ADDRESS),
        abi=orjson.loads(
            b'[{"constant":true,"inputs":[{"name":"","type":"bytes32"}],"name":"payoutDenominator","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"","type":"bytes32"},{"name":"","type":"uint256"}],"name":"payoutNumerators","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"","type":"address"},{"name":"","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')
    )
    cond_bytes = w3.to_bytes(hexstr=condition_id)

    print("[*] [Cashout] Waiting for Oracle to resolve market...")
    while True:
        denom = await asyncio.to_thread(ctf_contract.functions.payoutDenominator(cond_bytes).call)
        if denom > 0:
            break
        await asyncio.sleep(60)

    # 3. Check Winning Index properly
    index = 0 if side_name == "UP" else 1
    payout_num = await asyncio.to_thread(ctf_contract.functions.payoutNumerators(cond_bytes, index).call)
    
    if payout_num == 0:
        print("[-] [Cashout] Market Resolved: Result was a LOSS.")
        tg_utils.update_trade_pnl(condition_id, -spend_usdt)
        tg_utils.send_tg_msg(f"❌ <b>MARKET RESOLVED: LOST</b>\nTokens for {condition_id[:8]}... expired worthless.\nPnL: <b>-${spend_usdt:.2f} USDC</b>")
        return

    # We won! Calculate actual payload value and precise PnL 
    balance = await asyncio.to_thread(
        ctf_contract.functions.balanceOf(w3.to_checksum_address(funder), int(bought_token_id)).call
    )
    payout_usdc = balance / 1e6
    pnl = payout_usdc - spend_usdt
    print(f"[+] [Cashout] Winning position confirmed: {payout_usdc} USDC potential.")

    # 4. Prepare Redemption Data
    redeem_abi = [{"name": "redeemPositions", "type": "function",
                   "inputs": [{"name": "collateralToken", "type": "address"},
                              {"name": "parentCollectionId", "type": "bytes32"},
                              {"name": "conditionId", "type": "bytes32"}, {"name": "indexSets", "type": "uint256[]"}]}]
    call_data = w3.eth.contract(abi=redeem_abi).encode_abi(
        "redeemPositions",
        [w3.to_checksum_address(USDC_E_ADDRESS), b'\x00' * 32, cond_bytes, [1, 2]]
    )

    # 5. Initialize Relayer Client
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

    # 6. Execute Gasless Transaction using Correct Object Types
    try:
        # Wrap the payload in the individual SafeTransaction class
        tx = SafeTransaction(
            to=w3.to_checksum_address(CTF_ADDRESS),
            operation=OperationType.Call,
            data=call_data,
            value="0"  # Passed as a string to satisfy the SDK's strict typing
        )

        print("[*] [Cashout] Sending gasless redemption via Relayer...")

        # relayer.execute expects a list of SafeTransaction objects
        response = await asyncio.to_thread(relayer.execute, [tx], metadata="Redeem Winings")

        if response and hasattr(response, 'transaction_hash'):
            print(f"\n[$$$] CASHOUT SUCCESS! Hash: {response.transaction_hash}")
            tg_utils.update_trade_pnl(condition_id, pnl)
            tg_utils.send_tg_msg(f"✅ <b>MARKET RESOLVED: WON!</b>\nPayout: <b>${payout_usdc:.2f} USDC</b>\nPnL: <b>+${pnl:.2f} USDC</b>")
        else:
            print(f"[-] Cashout failed. Relayer response: {response}")
            tg_utils.update_trade_pnl(condition_id, pnl)
            tg_utils.send_tg_msg(f"⚠️ <b>WON (Failed Auto-Cashout)</b>\nPayout: <b>${payout_usdc:.2f} USDC</b>\nPnL: <b>+${pnl:.2f} USDC</b>")

    except Exception as e:
        err_msg = str(e)
        
        # Clean up the Polymarket API Exception fluff
        match = re.search(r"error_message=\{?'error':\s*'([^']+)'", err_msg)
        if match:
            clean_err = match.group(1)
        else:
            # Fallback for unexpected errors
            clean_err = err_msg[:60] + "..." if len(err_msg) > 60 else err_msg
            
        if "quota exceeded" in err_msg.lower():
            print(f"[!] Relayer Quota Exhausted: {clean_err}")
            tg_utils.update_trade_pnl(condition_id, pnl)
            tg_utils.send_tg_msg(f"⛔️ <b>CASHOUT BLOCKED (Rate Limit)</b>\nPayout: <b>${payout_usdc:.2f} USDC</b>\n<i>Error: {clean_err}</i>")
        else:
            print(f"[!] Cashout execution failed: {clean_err}")
            tg_utils.update_trade_pnl(condition_id, pnl)
            tg_utils.send_tg_msg(f"⚠️ <b>WON (Failed Auto-Cashout)</b>\nPayout: <b>${payout_usdc:.2f} USDC</b>\n<i>Error: {clean_err}</i>")


def cashout_background(condition_id: str, bought_token_id: str, end_date_iso: str, side_name: str, spend_usdt: float):
    return asyncio.create_task(_cashout_task(condition_id, bought_token_id, end_date_iso, side_name, spend_usdt))