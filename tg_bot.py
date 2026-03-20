import os
import logging
import asyncio
import tg_utils
import trading_core as tc
from dotenv import load_dotenv

# --- REST API Imports ---
from aiogram import Bot, Dispatcher, types, Router
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- Setup ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("❌ No TG_BOT_TOKEN found in .env!")

AUTHORIZED_IDS = [x.strip() for x in str(os.getenv("TG_CHAT_ID", "")).split(",") if x.strip()]

# 1. Initialize a Router globally instead of the Dispatcher
router = Router()


# --- Helpers ---
def is_auth(sender_id):
    if str(sender_id) in AUTHORIZED_IDS:
        return True
    print(f"[!] Unauthorized access attempt from: {sender_id}")
    return False


# --- Handlers ---
@router.message(Command("start"))
async def start(message: types.Message):
    print(f"[*] /start received from {message.from_user.id}")
    await message.answer(
        f"👋 Welcome to 1xBot !\n"
        f"Your Chat ID: <code>{message.from_user.id}</code>"
    )


@router.message(Command("status"))
async def status(message: types.Message):
    if not is_auth(message.from_user.id): return
    paused = tg_utils.is_bot_paused()
    status_msg = "⏸️ PAUSED" if paused else "🟢 RUNNING"
    await message.reply(f"System Status: {status_msg}")


@router.message(Command("resume"))
async def resume(message: types.Message):
    if not is_auth(message.from_user.id): return
    tg_utils.set_bot_status("running")
    await message.reply("▶️ Trader Bot resumed.")


@router.message(Command("pause"))
async def pause(message: types.Message):
    if not is_auth(message.from_user.id): return
    parts = message.text.split()
    minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    tg_utils.set_bot_status("paused", minutes)
    msg = f"⏸️ Paused for {minutes}m" if minutes > 0 else "⏸️ Paused indefinitely."
    await message.reply(msg)


@router.message(Command("balance"))
async def balance(message: types.Message):
    if not is_auth(message.from_user.id): return
    wait_msg = await message.reply("⏳ Syncing Wallet...")
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
        funder = os.getenv("POLY_FUNDER_ADDRESS")
        usdc_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
                     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
        usdc = w3.eth.contract(address=w3.to_checksum_address(tc.USDC_E_ADDRESS), abi=usdc_abi)
        bal = usdc.functions.balanceOf(w3.to_checksum_address(funder)).call()

        await wait_msg.edit_text(f"💰 Balance: <b>${bal / 1e6:.2f} USDC</b>")
    except Exception as e:
        await wait_msg.edit_text(f"[-] Error: {e}")


@router.message(Command("trades"))
async def trades(message: types.Message):
    if not is_auth(message.from_user.id): return
    recent = tg_utils.get_recent_trades(10)
    if not recent:
        await message.reply("No recent trades found.")
        return

    lines = ["<b>Last 10 Trades:</b>"]
    for t in recent:
        pnl = t.get('pnl')
        pnl_str = f" [PNL: {pnl:.2f}]" if pnl is not None else " [Pending]"
        lines.append(f"• {t['time_str']}: {t['action']} {t['side']} @ ${t['price']}{pnl_str}")

    await message.reply("\n".join(lines))


@router.message(Command("help"))
async def bot_help(message: types.Message):
    if not is_auth(message.from_user.id): return
    help_text = (
        "🤖 <b>1xBot Commands</b> 🤖\n\n"
        "• /status - Check if trader is running or paused\n"
        "• /balance - Check Proxy Wallet USDC balance\n"
        "• /trades - View last 10 trades & Oracle PNL\n"
        "• /pause <i>[mins]</i> - Pause for <i>X</i> minutes (or indefinitely)\n"
        "• /resume - Resume trading instantly\n"
        "• /help - Show this guide"
    )
    await message.reply(help_text)


# --- Debug Monitor ---
@router.message()
async def monitor(message: types.Message):
    if message.text:
        print(f"[*] RAW UPDATE CAUGHT: '{message.text}' from {message.from_user.id}")


# --- Async Execution ---
async def main():
    print("[*] Starting REST API Bot (aiogram)...")
    try:
        tg_utils.init_files()
    except Exception as e:
        print(f"[!] Warning on init_files: {e}")

    # 2. Initialize Bot and Dispatcher INSIDE the event loop
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # 3. Attach the global router to the local dispatcher
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    print("[+] Bot fully connected! SEND A BRAND NEW MESSAGE NOW.")
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())