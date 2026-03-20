import os
import asyncio
import time
import math
import gc
import orjson
import websockets
import aiohttp
from datetime import datetime, timezone
import uvloop

import trading_core as tc
import tg_utils

from dotenv import load_dotenv

load_dotenv()

# --- Strategy Configurables ---
TRADE_USDT = float(os.getenv("TRADE_USDT", "2.0"))
MAX_REMAINING_TIME = int(os.getenv("MAX_REMAINING_TIME", "298"))
MIN_REMAINING_TIME = int(os.getenv("MIN_REMAINING_TIME", "0"))
API_COOLDOWN = float(os.getenv("API_COOLDOWN", "1.0"))
MAX_ENTRY_PRICE = float(os.getenv("MAX_ENTRY_PRICE", "0.99"))
CASHOUT_INTERVAL_MINS = int(os.getenv("CASHOUT_INTERVAL_MINS", "15"))

# --- Hourly Target Height % ---
HOURLY_HEIGHT_PCT = {
    0: 0.03, 1: 0.08, 2: 0.12, 3: 0.06, 4: 0.05, 5: 0.09,
    6: 0.02, 7: 0.01, 8: 0.02, 9: 0.01, 10: 0.09, 11: 100.0,
    12: 0.02, 13: 0.10, 14: 0.03, 15: 0.20, 16: 0.12, 17: 0.12,
    18: 0.14, 19: 0.23, 20: 0.06, 21: 0.04, 22: 0.03, 23: 0.08
}

# --- Caching Configurables ---
FETCH_INTERVALS = int(os.getenv("PRESIGN_INTERVALS", "48"))


class PolyTrader:
    def __init__(self):
        self.running = True
        self.session = None

        # --- CACHING & BATCHING STATE ---
        self.cached_markets = {}
        self.fetching_markets = False
        self.pending_cashouts = []  # Added to track unresolved trades

        # Current Event Data
        self.slug = None
        self.condition_id = None
        self.start_ts = 0
        self.expiry_ts = 0
        self.yes_token = None
        self.no_token = None

        # Live Pricing State
        self.btc_open = None
        self.current_btc_px = None
        self.req_pct = None
        self.fetching_open = False

        # Division-Free Hot Path Triggers
        self.trigger_up_price = None
        self.trigger_down_price = None

        # Trade & Safety State
        self.trade_executed = False
        self.trade_failed = False
        self.trade_lock = False
        self.last_trade_attempt = 0.0

        self.owned_token_id = None
        self.owned_side_name = None
        self.trade_size_tokens = 0

    async def fetch_exact_binance_open(self, start_ts: int):
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "5m", "startTime": start_ts * 1000, "limit": 1}

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params, timeout=3) as resp:
                    data = await resp.json()
                    if data and len(data) > 0 and data[0][0] == start_ts * 1000:
                        self.btc_open = float(data[0][1])
                        self.trigger_up_price = self.btc_open * (1 + self.req_pct)
                        self.trigger_down_price = self.btc_open * (1 - self.req_pct)

                        print(f"\n[+] Synced Exact 5m Open Price: ${self.btc_open:.2f}")
                        print(
                            f"[*] Triggers Locked -> UP: >${self.trigger_up_price:.2f} | DOWN: <${self.trigger_down_price:.2f}")
                        return
            except Exception as e:
                print(f"[-] Binance Open API Error (Attempt {attempt + 1}/3): {e}")
            await asyncio.sleep(1)

        print("[-] Failed to fetch exact 5m open from Binance API. Defaulting to None.")
        self.btc_open = None
        self.trigger_up_price = None
        self.trigger_down_price = None

    def cleanup_old_orders(self):
        now = time.time()
        expired_keys = [ts for ts in self.cached_markets.keys() if ts < now - 300]
        for k in expired_keys:
            del self.cached_markets[k]
        if expired_keys:
            print(f"[*] Cache Cleanup: Purged {len(expired_keys)} expired markets from memory.")

    async def fetch_upcoming_markets(self):
        self.fetching_markets = True
        msg = f"\n[⚙️] Pausing trading to fetch up to {FETCH_INTERVALS} upcoming markets..."
        print(msg)
        tg_utils.send_tg_msg(msg)
        url = "https://gamma-api.polymarket.com/events"
        now = time.time()
        start_ts = math.floor(now / 300) * 300
        success_count = 0

        for i in range(FETCH_INTERVALS):
            ts = start_ts + (i * 300)
            if ts in self.cached_markets or ts + 300 < time.time(): continue

            check_slug = f"btc-updown-5m-{ts}"
            try:
                async with self.session.get(url, params={"slug": check_slug}) as resp:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        m = data[0].get("markets", [])[0]
                        t_ids = orjson.loads(m.get("clobTokenIds", "[]"))
                        self.cached_markets[ts] = {"condition_id": m.get("conditionId"), "yes_token": t_ids[0],
                                                   "no_token": t_ids[1]}
                        success_count += 1
                        print(f"  [+] Cached Market IDs: {check_slug}")
                    else:
                        print(f"  [*] Reached end of Polymarket's generated batches.")
                        break
            except Exception as e:
                print(f"  [-] API Fetch Error for {check_slug}: {e}")
            await asyncio.sleep(0.1)
        msg = f"[⚙️] Resume: Cached {success_count} new markets.\n"
        print(msg)
        tg_utils.send_tg_msg(msg)
        self.fetching_markets = False

    async def fetch_active_market(self):
        print("\n[*] Synchronizing with Cache Schedule...")
        while self.running:
            if self.fetching_markets:
                await asyncio.sleep(1)
                continue

            now = time.time()
            start_ts = math.floor(now / 300) * 300
            if now - start_ts > 290: start_ts += 300

            if start_ts in self.cached_markets:
                market_data = self.cached_markets[start_ts]
                self.slug = f"btc-updown-5m-{start_ts}"
                self.condition_id = market_data["condition_id"]
                self.yes_token = market_data["yes_token"]
                self.no_token = market_data["no_token"]
                self.start_ts = start_ts
                self.expiry_ts = start_ts + 300

                self.btc_open = None
                self.trigger_up_price = None
                self.trigger_down_price = None
                self.fetching_open = False
                self.trade_executed = self.trade_failed = self.trade_lock = False

                event_dt = datetime.fromtimestamp(self.start_ts, tz=timezone.utc)
                target_pct = HOURLY_HEIGHT_PCT.get(event_dt.hour, 0.12)
                self.req_pct = target_pct / 100.0

                print(f"[+] Locked Event (From Cache): {self.slug} | Target Ht: {target_pct}%")
                return True
            else:
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

                        if self.trigger_up_price and not self.fetching_markets:
                            await self.evaluate_strategy()
            except Exception:
                await asyncio.sleep(2)

    async def fetch_exact_kraken_open(self, start_ts: int):
        url = "https://api.kraken.com/0/public/OHLC"
        # XBTUSD is Kraken's standard ticker for BTC/USD. Interval 5 = 5 minutes.
        params = {"pair": "XBTUSD", "interval": "5", "since": start_ts - 60}

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params, timeout=3) as resp:
                    data = await resp.json()

                    # Kraken wraps successful responses in 'result' and errors in 'error'
                    if not data.get("error") and "result" in data:
                        # The pair key inside 'result' might be 'XXBTZUSD' depending on the API's mood
                        pair_key = list(data["result"].keys())[0]
                        if pair_key != 'last':  # Ignore the 'last' timestamp key
                            candles = data["result"][pair_key]

                            # Find the specific candle that perfectly matches our start_ts
                            for candle in candles:
                                candle_ts = int(candle[0])
                                if candle_ts == start_ts:
                                    self.btc_open = float(candle[1])
                                    self.trigger_up_price = self.btc_open * (1 + self.req_pct)
                                    self.trigger_down_price = self.btc_open * (1 - self.req_pct)

                                    print(f"\n[+] Synced Exact 5m Open Price (Kraken): ${self.btc_open:.2f}")
                                    print(
                                        f"[*] Triggers Locked -> UP: >${self.trigger_up_price:.2f} | DOWN: <${self.trigger_down_price:.2f}")
                                    return

                            print(f"  [*] Kraken Open API: Candle for {start_ts} not formed yet. Retrying...")
            except Exception as e:
                print(f"[-] Kraken Open API Error (Attempt {attempt + 1}/3): {e}")

            await asyncio.sleep(1)

        print("[-] Failed to fetch exact 5m open from Kraken API. Defaulting to None.")
        self.btc_open = None
        self.trigger_up_price = None
        self.trigger_down_price = None

    async def kraken_stream(self):
        url = "wss://ws.kraken.com/v2"
        sub_msg = {
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": ["BTC/USD"],
                "event_trigger": "bbo"
            }
        }

        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    print("[*] Kraken WS Connected (BTC/USD).")
                    await ws.send(orjson.dumps(sub_msg).decode("utf-8"))

                    while self.running:
                        msg = await ws.recv()
                        data = orjson.loads(msg)

                        if data.get("channel") == "ticker" and data.get("type") in ("snapshot", "update"):
                            ticker_info = data["data"][0]
                            # Calculate the new mid-price
                            new_mid = (float(ticker_info["bid"]) + float(ticker_info["ask"])) / 2.0

                            # Filter out volume-only changes (phantom updates)
                            if new_mid != self.current_btc_px:
                                self.current_btc_px = new_mid

                                if self.trigger_up_price and not self.fetching_markets:
                                    await self.evaluate_strategy()

            except Exception as e:
                print(f"[-] Kraken WS Stream Error: {e}")
                await asyncio.sleep(2)

    async def fetch_exact_binance_futures_open(self, start_ts: int):
        # Note the 'fapi' endpoint instead of 'api/v3'
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": "BTCUSDT", "interval": "5m", "startTime": start_ts * 1000, "limit": 1}

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params, timeout=3) as resp:
                    data = await resp.json()

                    if data and len(data) > 0 and data[0][0] == start_ts * 1000:
                        self.btc_open = float(data[0][1])
                        self.trigger_up_price = self.btc_open * (1 + self.req_pct)
                        self.trigger_down_price = self.btc_open * (1 - self.req_pct)

                        print(f"\n[+] Synced Exact 5m Open Price (Binance Futures): ${self.btc_open:.2f}")
                        print(
                            f"[*] Triggers Locked -> UP: >${self.trigger_up_price:.2f} | DOWN: <${self.trigger_down_price:.2f}")
                        return
            except Exception as e:
                print(f"[-] Binance Futures Open API Error (Attempt {attempt + 1}/3): {e}")
            await asyncio.sleep(1)

        print("[-] Failed to fetch exact 5m open from Binance Futures API. Defaulting to None.")
        self.btc_open = None
        self.trigger_up_price = None
        self.trigger_down_price = None

    async def binance_futures_stream(self):
        # Note the 'fstream' endpoint instead of 'stream'
        url = "wss://fstream.binance.com/ws/btcusdt@bookTicker"

        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    print("[*] Binance Futures WS Connected (BTCUSDT).")

                    while self.running:
                        msg = await ws.recv()
                        data = orjson.loads(msg)

                        # Calculate mid-price from best bid ('b') and best ask ('a')
                        new_mid = (float(data['b']) + float(data['a'])) / 2.0

                        # Kept the optimization here: only evaluate if the price actually shifted
                        if new_mid != self.current_btc_px:
                            self.current_btc_px = new_mid

                            if self.trigger_up_price and not self.fetching_markets:
                                await self.evaluate_strategy()

            except Exception as e:
                print(f"[-] Binance Futures WS Stream Error: {e}")
                await asyncio.sleep(2)

    async def strategy_heartbeat(self):
        print("[*] Strategy Heartbeat Started (10Hz)")
        while self.running:
            if self.fetching_markets:
                await asyncio.sleep(0.5)
                continue
            if self.start_ts and time.time() >= self.start_ts and self.btc_open is None and not getattr(self,
                                                                                                        'fetching_open',
                                                                                                        False):
                self.fetching_open = True
                asyncio.create_task(self.fetch_exact_binance_open(self.start_ts))
                #asyncio.create_task(self.fetch_exact_kraken_open(self.start_ts))
                #asyncio.create_task(self.fetch_exact_binance_futures_open(self.start_ts))
            await asyncio.sleep(0.05)

    async def keep_clob_warm(self):
        print("[*] CLOB Keep-Alive Pinger Started.")
        while self.running:
            try:
                await asyncio.to_thread(tc.get_client().get_ok)
            except Exception:
                pass
            await asyncio.sleep(20)

    async def evaluate_strategy(self):
        if not self.trigger_up_price or not self.trigger_down_price: return
        if self.trade_lock or (time.time() - self.last_trade_attempt < API_COOLDOWN) or self.current_btc_px == 0: return

        rem_time = int(self.expiry_ts - time.time())
        if not self.trade_executed and not self.trade_failed and MIN_REMAINING_TIME < rem_time < MAX_REMAINING_TIME:
            if self.current_btc_px > self.trigger_up_price:
                self.trade_lock = True
                await self.execute_trade(self.yes_token, "UP")
            elif self.current_btc_px < self.trigger_down_price:
                self.trade_lock = True
                await self.execute_trade(self.no_token, "DOWN")

    async def execute_trade(self, token_id, side_name):
        self.last_trade_attempt = time.time()
        print(f"[*] 🚀 TRIGGGER: Sending LIVE SDK BUY for {side_name} @ Limit {MAX_ENTRY_PRICE:.2f}")

        try:
            t0 = time.time()
            resp = await tc.buy(token_id, MAX_ENTRY_PRICE, TRADE_USDT)
            exec_time = (time.time() - t0) * 1000

            success = resp and resp.get("success")

            # --- GHOST FILL / TIMEOUT CHECK (UPDATED: Active Polling) ---
            if not success:
                print(f"[-] Trade API failed or timed out. Verifying on-chain settlement (polling for up to 45s)...")

                # Poll 9 times, every 5 seconds
                for attempt in range(1, 10):
                    await asyncio.sleep(5)
                    balance_tokens = await tc.get_token_balance(token_id)

                    if balance_tokens > 0:
                        print(
                            f"[+] Ghost Fill Detected on attempt {attempt}! Verified {balance_tokens:.2f} tokens in wallet.")
                        success = True
                        # Reconstruct response so downstream logic works perfectly
                        resp = {"success": True, "takingAmount": balance_tokens, "makingAmount": TRADE_USDT}
                        break
                    else:
                        print(f"  [*] Attempt {attempt}/9: Balance is 0. Waiting for relayer/RPC to sync...")

                # If it still fails after 45 seconds
                if not success:
                    err = resp.get("error_message") or resp.get("error") if isinstance(resp,
                                                                                       dict) else "Timeout/Unknown"
                    print(f"[-] Execution Confirmed Failed after verification: {err}. Skipping event.")
                    self.trade_failed = True
                    self.trade_lock = False
                    return

            # --- TRADE SUCCESS ROUTINE ---
            if success:
                self.trade_executed = True
                self.owned_token_id, self.owned_side_name = token_id, side_name
                self.trade_size_tokens = float(resp.get("takingAmount", 0))

                spend = float(resp.get('makingAmount', 0))
                fill_price = spend / self.trade_size_tokens if self.trade_size_tokens > 0 else 0.0
                print(
                    f"[+] Trade Success in {exec_time:.0f}ms! Spent ~${spend:.2f} for {self.trade_size_tokens} tokens.")

                tg_utils.log_trade(self.condition_id, "BUY", side_name, fill_price, self.trade_size_tokens)
                tg_utils.send_tg_msg(
                    f"🚨 <b>ENTRY: {side_name}</b>\nFill: ${fill_price:.3f} | Tokens: {self.trade_size_tokens:.1f}\nSpend: ${spend:.2f}"
                )

                # Queue for Batched Cashout later
                self.pending_cashouts.append({
                    "condition_id": self.condition_id,
                    "token_id": token_id,
                    "side_name": side_name,
                    "spend": spend,
                    "expiry_ts": self.expiry_ts
                })

        except Exception as e:
            print(f"[-] Exception during execution: {e}. Skipping remainder of event.")
            self.trade_failed = True
        finally:
            self.trade_lock = False

    async def batch_cashout_loop(self):
        """Periodically processes all unresolved trades strictly to preserve relayer quotas."""
        print(f"[*] Batch Cashout Loop Started (Executing every {CASHOUT_INTERVAL_MINS} mins).")
        while self.running:
            await asyncio.sleep(CASHOUT_INTERVAL_MINS * 60)
            if self.pending_cashouts:
                msg = f"\n[$$$] Initiating Batch Cashout for {len(self.pending_cashouts)} pending positions..."
                print(msg)
                tg_utils.send_tg_msg(msg)
                # The function handles verification and returns any that are STILL pending (e.g. unresolved by oracle)
                self.pending_cashouts = await tc.process_batch_cashouts(self.pending_cashouts)

    async def run(self):
        print("=== Polymarket BTC 5m Trader (Batched Cashouts + Fail Safes) ===")
        self.session = aiohttp.ClientSession()
        await self.fetch_upcoming_markets()

        asyncio.create_task(self.binance_stream())
        #asyncio.create_task(self.kraken_stream())
        #asyncio.create_task(self.binance_futures_stream())
        asyncio.create_task(self.strategy_heartbeat())
        asyncio.create_task(self.keep_clob_warm())
        asyncio.create_task(self.batch_cashout_loop())

        while self.running:
            if tg_utils.is_bot_paused():
                print("[*] Trader is PAUSED. Sleeping for 30s...")
                await asyncio.sleep(30)
                continue

            self.cleanup_old_orders()
            future_intervals = [ts for ts in self.cached_markets.keys() if ts > time.time()]
            if len(future_intervals) < 3 and not self.fetching_markets:
                await self.fetch_upcoming_markets()

            await self.fetch_active_market()

            gc.disable()
            print("[*] Garbage Collection Disabled for critical trading window.")

            wait_time = self.expiry_ts - time.time()
            if wait_time > 0: await asyncio.sleep(wait_time)

            if self.trade_executed:
                self.cached_markets.pop(self.start_ts, None)

            gc.enable()
            gc.collect()
            print("[*] Garbage Collection Re-enabled and memory purged.")
            print("\n--- Window Expired. Cycling... ---")
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    trader = PolyTrader()
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        trader.running = False