import os
import json
import time
import requests
from filelock import FileLock
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

CONTROL_FILE = "bot_control.json"
HISTORY_FILE = "trade_history.json"

# --- Initialization ---
def init_files():
    """Ensure the IPC state files exist."""
    if not os.path.exists(CONTROL_FILE):
        with open(CONTROL_FILE, 'w') as f:
            json.dump({"status": "running", "pause_until": 0}, f)
            
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'w') as f:
            json.dump([], f)

# --- Telegram REST Messenger ---
def send_tg_msg(text: str):
    """Sends a synchronous Telegram message using the HTTP REST API."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[!] TG_BOT_TOKEN or TG_CHAT_ID missing in .env. Cannot send msg.")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chat_ids = [x.strip() for x in str(TG_CHAT_ID).split(",") if x.strip()]

    for cid in chat_ids:
        payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"[-] Failed to send Telegram message to {cid}: {e}")

# --- State Management (Read/Write) ---
def is_bot_paused() -> bool:
    """Reads bot_control.json to determine if the trader should skip execution."""
    try:
        with open(CONTROL_FILE, 'r') as f:
            data = json.load(f)
            
        if data.get("status") == "paused":
            pause_until = data.get("pause_until", 0)
            if pause_until > 0 and time.time() > pause_until:
                # Pause expired! Auto-resume
                set_bot_status("running")
                send_tg_msg("▶️ Pause expired. Bot automatically resumed.")
                return False
            return True
        return False
    except Exception:
        return False # Default to running on error

def set_bot_status(status: str, duration_minutes: int = 0):
    """Updates bot_control.json state."""
    init_files()
    pause_until = (time.time() + (duration_minutes * 60)) if status == "paused" and duration_minutes > 0 else 0
    with open(CONTROL_FILE, 'w') as f:
        json.dump({"status": status, "pause_until": pause_until}, f)

# --- Trade History Logging ---
def log_trade(condition_id: str, action: str, side: str, price: float, size: float):
    """Appends a trade to trade_history.json safely using a file lock."""
    init_files()
    lock = FileLock(f"{HISTORY_FILE}.lock", timeout=5)
    
    trade_entry = {
        "timestamp": int(time.time()),
        "time_str": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
        "condition_id": condition_id,
        "action": action, # 'BUY', 'SELL', 'CASHOUT'
        "side": side,     # 'UP', 'DOWN'
        "price": price,
        "size": size,
        "pnl": None
    }
    
    with lock:
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except Exception:
            history = []
            
        history.append(trade_entry)
        
        # Keep only last 50 trades to limit file size
        if len(history) > 50:
            history = history[-50:]
            
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=4)

def update_trade_pnl(condition_id: str, pnl_amount: float):
    """Finds the most recent trade matching the condition_id and sets its final PNL."""
    init_files()
    lock = FileLock(f"{HISTORY_FILE}.lock", timeout=5)
    
    with lock:
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
                
            # Search backwards for the most recent matching trade
            updated = False
            for trade in reversed(history):
                if trade.get("condition_id") == condition_id:
                    trade["pnl"] = pnl_amount
                    updated = True
                    break
                    
            if updated:
                with open(HISTORY_FILE, 'w') as f:
                    json.dump(history, f, indent=4)
        except Exception as e:
            print(f"[-] Failed to update PNL history: {e}")

def get_recent_trades(limit: int = 5) -> list:
    """Reads the trade history file for the Telegram bot to display."""
    init_files()
    try:
        with open(HISTORY_FILE, 'r') as f:
            history = json.load(f)
        return history[-limit:]
    except Exception:
        return []

init_files()
