import os
import asyncio
import time
import math
import orjson
import websockets
import aiohttp
import requests
import socket
from datetime import datetime, timezone

import trading_core as tc
import tg_utils

from dotenv import load_dotenv
load_dotenv()

# --- Strategy Configurables ---
TRADE_USDT = float(os.getenv("TRADE_USDT", "5.0"))               # Target amount to spend in USDT
VOLATILE_PCT = float(os.getenv("VOLATILE_PCT", "0.00225"))       # Minimum height percentage required between 12-20 UTC (e.g., 0.225%)
NORMAL_PCT = float(os.getenv("NORMAL_PCT", "0.0008"))            # Minimum height percentage required otherwise (e.g., 0.08%)
MAX_REMAINING_TIME = int(os.getenv("MAX_REMAINING_TIME", "120"))  # Max remaining seconds in the 5m event to allow an entry
MIN_REMAINING_TIME = int(os.getenv("MIN_REMAINING_TIME", "5"))   # Min remaining seconds (don't enter at the last second)
API_COOLDOWN = float(os.getenv("API_COOLDOWN", "1.0"))           # Cooldown to prevent rapid-fire failures
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.97"))    # ABSOLUTE SAFETY: Max acceptable entry price


class PolyTrader:
    def __init__(self):
        self.running = True
        self.session = None

        # Current Event Data
        self.slug = None
        self.condition_id = None
        self.start_ts = 0
        self.expiry_ts = 0
        self.yes_token = None
        self.no_token = None

        # Live Pricing State
        self.btc_open = None
        self.current_btc_px = 0.0
        self.req_pct = 0.0

        # { token_id: { "b": { price: size }, "a": { price: size } } }
        self.orderbooks = {}

        # Trade & Safety State
        self.trade_executed = False
        self.trade_failed = False
        self.trade_lock = False
        self.last_trade_attempt = 0.0

        self.owned_token_id = None
        self.owned_side_name = None
        self.trade_size_tokens = 0

    async def fetch_binance_open_rest(self, start_ts: int):
        """Fallback: Fetches the 1m Open price from Binance REST API if stream missed it."""
        if self.btc_open is not None:
            return

        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": start_ts * 1000, "limit": 1}
        try:
            async with self.session.get(url, params=params) as resp:
                data = await resp.json()
                if data and len(data) > 0 and self.btc_open is None:
                    self.btc_open = float(data[0][1])
                    print(f"[+] Mid-Event Recovery: Fetched REST Open Price: ${self.btc_open:.2f}")
                    
                    target_height = self.btc_open * self.req_pct
                    print(f"[*] Target Shift Needed: ±${target_height:.2f}")
        except Exception as e:
            print(f"[-] Error fetching REST Open price: {e}")

    async def fetch_active_market(self):
        """Find the target market slug and tokens."""
        url = "https://gamma-api.polymarket.com/events"
        print("\n[*] Synchronizing with Polymarket Schedule...")
        while self.running:
            try:
                now = time.time()
                start_ts = math.floor(now / 300) * 300
                if now - start_ts > 290:
                    start_ts += 300

                check_slug = f"btc-updown-5m-{start_ts}"
                async with self.session.get(url, params={"slug": check_slug}) as resp:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        m = data[0].get("markets", [])[0]
                        t_ids = orjson.loads(m.get("clobTokenIds", "[]"))

                        self.slug = check_slug
                        self.condition_id = m.get("conditionId")
                        self.yes_token, self.no_token = t_ids[0], t_ids[1]
                        self.start_ts, self.expiry_ts = start_ts, start_ts + 300

                        self.btc_open = None
                        self.trade_executed = self.trade_failed = self.trade_lock = False

                        # Calculate and Print the Required Percentage Threshold for this market
                        event_dt = datetime.fromtimestamp(self.start_ts, tz=timezone.utc)
                        self.req_pct = VOLATILE_PCT if 12 <= event_dt.hour < 20 else NORMAL_PCT

                        print(f"[+] Locked Event: {self.slug}")

                        # Only trigger REST recovery if we are already past the start time
                        if time.time() > self.start_ts + 1:
                            await self.fetch_binance_open_rest(self.start_ts)
                        return True
                await asyncio.sleep(2)
            except Exception as e:
                print(f"[-] Market Fetch Error: {e}")
                await asyncio.sleep(2)

    async def binance_stream(self):
        url = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    print("[*] Binance WS Connected.")
                    while self.running:
                        msg = await ws.recv()
                        data = orjson.loads(msg)
                        self.current_btc_px = (float(data['b']) + float(data['a'])) / 2.0

                        # Primary: Capture open price exactly at the start of the interval
                        if self.start_ts and time.time() >= self.start_ts and self.btc_open is None:
                            self.btc_open = self.current_btc_px
                            print(f"\n[+] Synced :00 Open Price: ${self.btc_open:.2f}")
                            
                            target_height = self.btc_open * self.req_pct
                            print(f"[*] Target Shift Needed: ±${target_height:.2f}")

                        if self.btc_open:
                            pass # Strategy evaluation is now handled by the async heartbeat
            except Exception:
                await asyncio.sleep(2)

    async def strategy_heartbeat(self):
        """Runs continuously to decouple condition checking from websocket volume."""
        print("[*] Strategy Heartbeat Started (10Hz)")
        while self.running:
            if self.btc_open:
                await self.evaluate_strategy()
            await asyncio.sleep(0.05)

    async def evaluate_strategy(self):
        """Triggers the trade function if conditions are met."""
        if self.trade_lock or (time.time() - self.last_trade_attempt < API_COOLDOWN):
            return

        rem_time = int(self.expiry_ts - time.time())
        pct_delta = (self.current_btc_px - self.btc_open) / self.btc_open

        if not self.trade_executed and not self.trade_failed and MIN_REMAINING_TIME < rem_time < MAX_REMAINING_TIME:
            if pct_delta > self.req_pct:
                await self.execute_trade(self.yes_token, "UP")
            elif pct_delta < -self.req_pct:
                await self.execute_trade(self.no_token, "DOWN")

    async def execute_trade(self, token_id, side_name):
        """Atomic trade execution: Spends TRADE_USDT and tracks actual tokens received."""
        self.trade_lock = True
        self.last_trade_attempt = time.time()

        # 1. Fixed limit price for immediate FOK fill
        limit_price = MAX_ENTRY_PRICE
        
        # 2. Execution logic
        # For BUY orders, Polymarket interprets 'size' as the USDC amount to spend.
        print(f"[*] Attempting BUY: Spending ${TRADE_USDT} on {side_name} @ Fixed Limit {limit_price:.2f}")

        try:
            # We pass TRADE_USDT as the size (the 'making' amount)
            resp = await tc.buy(token_id, price=limit_price, size=TRADE_USDT)

            if resp and resp.get("success"):
                self.trade_executed = True
                self.owned_token_id, self.owned_side_name = token_id, side_name

                # IMPORTANT: Capture the EXACT number of tokens received from the response.
                # This is required so the Stop Loss (Sell) knows how many tokens to dump.
                self.trade_size_tokens = float(resp.get("takingAmount", 0))

                print(
                    f"[+] Trade Success! Spent ~${float(resp.get('makingAmount', 0)):.2f} for {self.trade_size_tokens} tokens.")
                
                # Calculate actual average fill price dynamically
                spend = float(resp.get('makingAmount', 0))
                fill_price = spend / self.trade_size_tokens if self.trade_size_tokens > 0 else 0.0
                    
                tg_utils.log_trade(self.condition_id, "BUY", side_name, fill_price, self.trade_size_tokens)
                tg_utils.send_tg_msg(f"🚨 <b>ENTRY: {side_name}</b>\nFill: ${fill_price:.3f} | Tokens: {self.trade_size_tokens:.1f}\nSpend: ${spend:.2f}")
            else:
                err = resp.get("error_message") if resp else "Unknown"
                print(f"[-] BUY Failed: {err}. Skipping remainder of event.")
                self.trade_failed = True
        except Exception as e:
            print(f"[-] Exception during execution: {e}. Skipping remainder of event.")
            self.trade_failed = True
        finally:
            self.trade_lock = False

    async def run(self):
        print("=== Polymarket BTC 5m Trader (Atomic Execution) ===")
        
        # Startup Diagnostics
        hostname = socket.gethostname()
        try:
            ip = requests.get('https://api.ipify.org', timeout=3).text
        except:
            ip = "Unknown"
            
        try:
            t0 = time.time()
            with socket.create_connection(("clob.polymarket.com", 443), timeout=3):
                pass
            ping = f"{int((time.time() - t0) * 1000)}ms"
        except:
            ping = "Unreachable"
            
        tg_utils.send_tg_msg(f"🟢 <b>Trader Bot Started</b>\nHost: {hostname}\nIP: {ip}\nPing (CLOB): {ping}\nTargeting: Polymarket 5m BTC\nStatus: Running")
        
        self.session = aiohttp.ClientSession()
        asyncio.create_task(self.binance_stream())
        asyncio.create_task(self.strategy_heartbeat())

        while self.running:
            if tg_utils.is_bot_paused():
                print("[*] Trader is PAUSED. Sleeping for 30s before checking again...")
                await asyncio.sleep(30)
                continue
                
            await self.fetch_active_market()

            wait_time = self.expiry_ts - time.time()
            if wait_time > 0: await asyncio.sleep(wait_time)

            if self.trade_executed:
                # Pass side name and spend for accurate Oracle logging
                utc_now_iso = datetime.now(timezone.utc).isoformat()
                tc.cashout_background(self.condition_id, self.owned_token_id, utc_now_iso, self.owned_side_name, TRADE_USDT)

            print("\n--- Window Expired. Cycling... ---")
            await asyncio.sleep(1)


if __name__ == "__main__":
    trader = PolyTrader()
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        trader.running = False