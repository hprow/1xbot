import os
import logging
import asyncio
import tg_utils
import trading_core as tc
from telethon import TelegramClient, events
from dotenv import load_dotenv

# --- Setup ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# CRITICAL: API_ID must be an integer for Telethon
try:
    API_ID = int(os.getenv("TG_API_ID", 0))
except ValueError:
    API_ID = 0

API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
AUTHORIZED_IDS = [x.strip() for x in str(os.getenv("TG_CHAT_ID", "")).split(",") if x.strip()]

# Initialize client but DO NOT start it globally
client = TelegramClient('bot_session', API_ID, API_HASH)


# --- Helpers ---
def is_auth(sender_id):
    if str(sender_id) in AUTHORIZED_IDS:
        return True
    print(f"[!] Unauthorized access attempt from: {sender_id}")
    return False


# --- Handlers ---
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    print(f"[*] /start received from {event.sender_id}")
    await event.respond(
        f"👋 Welcome to PolyTrader!\n"
        f"Your Chat ID: <code>{event.sender_id}</code>\n"
        f"Set this as TG_CHAT_ID in your .env",
        parse_mode='html'
    )


@client.on(events.NewMessage(pattern='/status'))
async def status(event):
    if not is_auth(event.sender_id): return
    paused = tg_utils.is_bot_paused()
    status_msg = "⏸️ PAUSED" if paused else "🟢 RUNNING"
    await event.reply(f"System Status: {status_msg}")


@client.on(events.NewMessage(pattern='/resume'))
async def resume(event):
    if not is_auth(event.sender_id): return
    tg_utils.set_bot_status("running")
    await event.reply("▶️ Trader Bot resumed.")


@client.on(events.NewMessage(pattern='/pause'))
async def pause(event):
    if not is_auth(event.sender_id): return
    parts = event.raw_text.split()
    minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    tg_utils.set_bot_status("paused", minutes)
    msg = f"⏸️ Paused for {minutes}m" if minutes > 0 else "⏸️ Paused indefinitely."
    await event.reply(msg)


@client.on(events.NewMessage(pattern='/balance'))
async def balance(event):
    if not is_auth(event.sender_id): return
    wait_msg = await event.reply("⏳ Syncing Wallet...")
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
        funder = os.getenv("POLY_FUNDER_ADDRESS")
        usdc_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
                     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
        usdc = w3.eth.contract(address=w3.to_checksum_address(tc.USDC_E_ADDRESS), abi=usdc_abi)
        bal = usdc.functions.balanceOf(w3.to_checksum_address(funder)).call()
        await wait_msg.edit(f"💰 Balance: <b>${bal / 1e6:.2f} USDC</b>", parse_mode='html')
    except Exception as e:
        await wait_msg.edit(f"[-] Error: {e}")


@client.on(events.NewMessage(pattern='/trades'))
async def trades(event):
    if not is_auth(event.sender_id): return
    recent = tg_utils.get_recent_trades(10)
    if not recent:
        await event.reply("No recent trades found.")
        return

    lines = ["<b>Last 10 Trades:</b>"]
    for t in recent:
        pnl = t.get('pnl')
        pnl_str = f" [PNL: {pnl:.2f}]" if pnl is not None else " [Pending]"
        lines.append(f"• {t['time_str']}: {t['action']} {t['side']} @ ${t['price']}{pnl_str}")

    await event.reply("\n".join(lines), parse_mode='html')


@client.on(events.NewMessage(pattern='/help'))
async def bot_help(event):
    if not is_auth(event.sender_id): return
    help_text = (
        "🤖 <b>PolyTrader Bot Commands</b> 🤖\n\n"
        "• /status - Check if trader is running or paused\n"
        "• /balance - Check Proxy Wallet USDC balance\n"
        "• /trades - View last 10 trades & Oracle PNL\n"
        "• /pause <i>[mins]</i> - Pause for <i>X</i> minutes (or indefinitely)\n"
        "• /resume - Resume trading instantly\n"
        "• /help - Show this guide"
    )
    await event.reply(help_text, parse_mode='html')


# --- Debug Monitor ---
@client.on(events.NewMessage)
async def monitor(event):
    """Catches absolutely everything to ensure connection is live."""
    print(f"[*] RAW UPDATE CAUGHT: '{event.raw_text}' from {event.sender_id}")


# --- Async Execution ---
async def main():
    print("[*] Starting Telethon Bot...")
    try:
        tg_utils.init_files()
    except Exception as e:
        print(f"[!] Warning on init_files: {e}")

    # Start the client INSIDE the event loop
    await client.start(bot_token=BOT_TOKEN)
    print("[+] Telethon fully connected. SEND A BRAND NEW MESSAGE NOW.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    # This properly initializes the Windows event loop
    asyncio.run(main())