import os
import asyncio
import time
import math
import json
import websockets
import aiohttp
import requests
import socket
from datetime import datetime, timezone
import traceback

import trading_core as tc
import tg_utils

from dotenv import load_dotenv

load_dotenv()

# --- Strategy Configurables ---
TRADE_USDT = float(os.getenv("TRADE_USDT", "5.0"))  # Target amount to spend in USDT
STOP_LOSS_PRICE = float(os.getenv("STOP_LOSS_PRICE", "0.2"))  # Minimum Bid price to trigger a stop loss
MIN_BTC_HEIGHT = float(os.getenv("MIN_BTC_HEIGHT", "55.0"))  # Minimum signed (BTC Current - BTC Open) required
MAX_REMAINING_TIME = int(
    os.getenv("MAX_REMAINING_TIME", "90"))  # Max remaining seconds in the 5m event to allow an entry
MIN_REMAINING_TIME = int(os.getenv("MIN_REMAINING_TIME", "5"))  # Min remaining seconds (don't enter at the last second)
API_COOLDOWN = float(os.getenv("API_COOLDOWN", "1.0"))  # Cooldown to prevent rapid-fire failures
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.97"))  # ABSOLUTE SAFETY: Max acceptable entry price


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

        # { token_id: { "b": { price: size }, "a": { price: size } } }
        self.orderbooks = {}

        # Trade & Safety State
        self.trade_executed = False
        self.trade_exited = False
        self.trade_failed = False
        self.stop_loss_failed = False
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
                        t_ids = json.loads(m.get("clobTokenIds", "[]"))

                        self.slug = check_slug
                        self.condition_id = m.get("conditionId")
                        self.yes_token, self.no_token = t_ids[0], t_ids[1]
                        self.start_ts, self.expiry_ts = start_ts, start_ts + 300

                        self.orderbooks = {self.yes_token: {"b": {}, "a": {}}, self.no_token: {"b": {}, "a": {}}}
                        self.btc_open = None
                        self.trade_executed = self.trade_exited = self.trade_failed = self.stop_loss_failed = self.trade_lock = False

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
                        data = json.loads(msg)
                        self.current_btc_px = (float(data['b']) + float(data['a'])) / 2.0

                        # Primary: Capture open price exactly at the start of the interval
                        if self.start_ts and time.time() >= self.start_ts and self.btc_open is None:
                            self.btc_open = self.current_btc_px
                            print(f"\n[+] Synced :00 Open Price: ${self.btc_open:.2f}")

                        if self.btc_open:
                            pass  # Strategy evaluation is now handled by the async heartbeat
            except Exception:
                await asyncio.sleep(2)

    async def polymarket_stream(self, stop_event: asyncio.Event):
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        while not stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps({"assets_ids": [self.yes_token, self.no_token], "type": "market"}))
                    while not stop_event.is_set():
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        data = json.loads(msg)
                        for item in (data if isinstance(data, list) else [data]):
                            etype = item.get("event_type") or item.get("event")
                            tid = item.get("asset_id") or item.get("market")
                            if etype == "book":
                                # Prune old ghost levels by clearing the local dict before applying the new snapshot
                                self.orderbooks[tid]["b"].clear()
                                self.orderbooks[tid]["a"].clear()
                                for b in item.get("bids", []): self.orderbooks[tid]["b"][float(b["price"])] = float(
                                    b["size"])
                                for a in item.get("asks", []): self.orderbooks[tid]["a"][float(a["price"])] = float(
                                    a["size"])
                            elif etype == "price_change":
                                for c in item.get("changes", item.get("price_changes", [])):
                                    side, px, sz = c["side"], float(c["price"]), float(c["size"])
                                    target = self.orderbooks[tid]["b"] if side == "BUY" else self.orderbooks[tid]["a"]
                                    if sz == 0:
                                        target.pop(px, None)
                                    else:
                                        target[px] = sz
                        # Strategy evaluation is now handled by the async heartbeat
            except Exception:
                if not stop_event.is_set(): await asyncio.sleep(2)

    async def strategy_heartbeat(self):
        """Runs continuously to decouple condition checking from websocket volume."""
        print("[*] Strategy Heartbeat Started (10Hz)")
        while self.running:
            if self.btc_open and self.yes_token in self.orderbooks:
                await self.evaluate_strategy()
            await asyncio.sleep(0.1)

    async def evaluate_strategy(self):
        """Triggers the trade function if conditions are met."""
        if self.trade_lock or (time.time() - self.last_trade_attempt < API_COOLDOWN):
            return

        rem_time = int(self.expiry_ts - time.time())
        btc_delta = self.current_btc_px - self.btc_open

        if not self.trade_executed and not self.trade_failed and MIN_REMAINING_TIME < rem_time < MAX_REMAINING_TIME:
            if btc_delta > MIN_BTC_HEIGHT:
                await self.execute_trade(self.yes_token, "UP")
            elif btc_delta < -MIN_BTC_HEIGHT:
                await self.execute_trade(self.no_token, "DOWN")

        elif self.trade_executed and not self.trade_exited and not self.stop_loss_failed:
            bid = max(self.orderbooks[self.owned_token_id]["b"].keys()) if self.orderbooks[self.owned_token_id][
                "b"] else 0.0
            if 0 < bid <= STOP_LOSS_PRICE:
                await self.execute_stop_loss()

    async def execute_trade(self, token_id, side_name):
        """Atomic trade execution: Spends TRADE_USDT and tracks actual tokens received."""
        self.trade_lock = True
        self.last_trade_attempt = time.time()

        # 1. Look up the absolute freshest price from the local orderbook
        try:
            # We add a tiny 0.01 buffer (slippage) to the limit price to ensure the FOK fills
            current_ask = min(self.orderbooks[token_id]["a"].keys()) if self.orderbooks[token_id]["a"] else 1.0
            limit_price = round(current_ask + 0.01, 2)
        except Exception:
            self.trade_lock = False
            return

        # 2. Safety Cutoff: If price jumped, ABORT
        if limit_price > MAX_ENTRY_PRICE:
            print(
                f"[!] ABORT: Price for {side_name} (incl. slippage) is {limit_price}, exceeding max {MAX_ENTRY_PRICE}.")
            self.trade_lock = False
            return

        # 3. Execution logic
        # For BUY orders, Polymarket interprets 'size' as the USDC amount to spend.
        print(f"[*] Attempting BUY: Spending ${TRADE_USDT} on {side_name} @ Limit {limit_price:.2f}")

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

                tg_utils.log_trade(self.condition_id, "BUY", side_name, limit_price, self.trade_size_tokens)
                tg_utils.send_tg_msg(
                    f"🚨 <b>ENTRY: {side_name}</b>\nPrice: ${limit_price:.2f} | Tokens: {self.trade_size_tokens:.1f}\nSpend: ${float(resp.get('makingAmount', 0)):.2f}")
            else:
                err = resp.get("error_message") if resp else "Unknown"
                print(f"[-] BUY Failed: {err}. Skipping remainder of event.")
                self.trade_failed = True
        except Exception as e:
            print(f"[-] Exception during execution: {e}. Skipping remainder of event.")
            self.trade_failed = True
        finally:
            self.trade_lock = False

    async def execute_stop_loss(self):
        self.trade_lock = True
        self.last_trade_attempt = time.time()
        print(f"[*] STOP LOSS: Dumping {self.owned_side_name} at market...")
        try:
            resp = await tc.sell(self.owned_token_id, price=0.01, size=self.trade_size_tokens)
            if resp and resp.get("success"):
                self.trade_exited = True
                print("[+] Stop Loss Executed.")
                tg_utils.log_trade(self.condition_id, "SELL", self.owned_side_name, 0.01, self.trade_size_tokens)
                tg_utils.send_tg_msg(
                    f"⚠️ <b>STOP LOSS TRIGGERED</b>\nDumped {self.trade_size_tokens} {self.owned_side_name} tokens at market to prevent further loss.")
            else:
                err = resp.get("error_message") if resp else "Unknown"
                print(f"[-] Stop Loss Failed: {err}. Abandoning stop loss attempts.")
                self.stop_loss_failed = True
        except Exception as e:
            print(f"[-] Exception during stop loss: {e}. Abandoning stop loss attempts.")
            self.stop_loss_failed = True
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

        tg_utils.send_tg_msg(
            f"🟢 <b>Trader Bot Started</b>\nHost: {hostname}\nIP: {ip}\nTargeting: Polymarket 5m BTC\nStatus: Running")

        self.session = aiohttp.ClientSession()
        asyncio.create_task(self.binance_stream())
        asyncio.create_task(self.strategy_heartbeat())

        while self.running:
            if tg_utils.is_bot_paused():
                print("[*] Trader is PAUSED. Sleeping for 30s before checking again...")
                await asyncio.sleep(30)
                continue

            await self.fetch_active_market()
            stop_event = asyncio.Event()
            poly_task = asyncio.create_task(self.polymarket_stream(stop_event))

            wait_time = self.expiry_ts - time.time()
            if wait_time > 0: await asyncio.sleep(wait_time)

            stop_event.set()
            await poly_task

            if self.trade_executed and not self.trade_exited:
                # FIX: Pass UTC-aware ISO format string to avoid naive vs aware comparison error
                utc_now_iso = datetime.now(timezone.utc).isoformat()
                tc.cashout_background(self.condition_id, self.owned_token_id, utc_now_iso)

            print("\n--- Window Expired. Cycling... ---")
            await asyncio.sleep(1)


if __name__ == "__main__":
    trader = PolyTrader()
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        trader.running = False