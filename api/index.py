#!/usr/bin/env python3
"""
Escrow Shield - Vercel-ready Telegram Escrow Bot
- Webhook mode (Flask) for deployment on Vercel
- SQLite storage for escrows and users
- OKX v5 signing for balance checks and withdrawals
- Inline keyboard UI (English + 简体中文)
- Admin-only control for confirmations & releases
- Reads secrets from environment variables
"""

import os
import time
import hmac
import hashlib
import base64
import json
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Any

import requests
from dotenv import load_dotenv
from flask import Flask, request

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ------------------ Load env ------------------
load_dotenv()  # local dev; in Vercel you'll set env vars in dashboard

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_ID") or 0)
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
DEPOSIT_ADDRESS = os.getenv("DEPOSIT_ADDRESS", "") or "Set_DEPOSIT_ADDRESS_IN_ENV"
SQLITE_FILE = os.getenv("SQLITE_FILE", "escrow_vercel.db")

# ------------------ Logging ------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("escrow_bot_vercel")

# ------------------ DB ------------------
def init_db(path: str = SQLITE_FILE):
    conn = sqlite3.connect(path, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY,
      telegram_id INTEGER UNIQUE,
      lang TEXT DEFAULT 'en',
      wallet TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS escrows (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      seeker_id INTEGER,
      provider_id INTEGER,
      provider_wallet TEXT,
      amount TEXT,
      currency TEXT DEFAULT 'USDT',
      status TEXT, -- created, paid, confirmed, released, cancelled
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      okx_tx_id TEXT,
      deposit_snapshot TEXT
    )""")
    conn.commit()
    return conn

DB = init_db(SQLITE_FILE)

# ------------------ Messages ------------------
MESSAGES = {
    "welcome": {"en": "Welcome to Escrow Shield! Choose language / 请选择语言", "zh": "欢迎使用 Escrow Shield！请选择语言"},
    "menu": {"en": "Choose an action:", "zh": "选择操作："},
    "setwallet_prompt": {"en": "Send your TRC20 wallet address (provider).", "zh": "请发送您的 TRC20 钱包地址（收款方）。"},
    "wallet_saved": {"en": "Wallet saved ✅", "zh": "钱包已保存 ✅"},
    "enter_amount": {"en": "Enter amount in USDT / 输入 USDT 数量:", "zh": "请输入 USDT 数量:"},
    "deposit_info": {"en": "Please send USDT (TRC20) to:\n{addr}\nThen tap 'Mark as Paid'.", "zh": "请将 USDT (TRC20) 转账到：\n{addr}\n然后点击「标记为已支付」。"},
    "paid_marked": {"en": "Payment marked as paid. Waiting for admin confirmation.", "zh": "已标记为已支付。正在等待管理员确认。"},
    "confirmed": {"en": "Escrow confirmed. Admin may release funds.", "zh": "托管已确认。管理员可以释放资金。"},
    "released": {"en": "Funds released ✅", "zh": "资金已释放 ✅"},
    "cancelled": {"en": "Escrow cancelled.", "zh": "托管已取消。"},
    "only_admin": {"en": "Only admin can do that.", "zh": "只有管理员可以执行此操作。"},
    "invalid_amount": {"en": "Invalid amount. Enter a numeric USDT amount (e.g. 10 or 10.5).", "zh": "无效金额。请输入数字格式的USDT金额（例如 10 或 10.5）。"},
    "okx_withdraw_failed": {"en": "OKX withdraw failed; please release manually. Response: {resp}", "zh": "OKX 提现失败；请手动释放。 响应：{resp}"},
    "escrow_created": {"en": "Escrow #{id} created for {amt} USDT. Send funds to the deposit address.", "zh": "托管 #{id} 已为 {amt} USDT 创建。请将资金发送到收款地址。"},
    "list_empty": {"en": "No escrows found.", "zh": "未找到托管。"}
}

def get_msg(key: str, lang: str = "en", **kwargs) -> str:
    t = MESSAGES.get(key, {}).get(lang) or MESSAGES.get(key, {}).get("en") or ""
    return t.format(**kwargs) if kwargs else t

# ------------------ OKX helpers ------------------
def okx_sign(timestamp: str, method: str, request_path: str, body: str, secret: str):
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_headers(method: str, request_path: str, body: Optional[dict] = None):
    ts = str(time.time())
    body_str = json.dumps(body) if body else ""
    sig = okx_sign(ts, method, request_path, body_str, OKX_API_SECRET or "")
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY or "",
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE or "",
        "Content-Type": "application/json"
    }
    return headers, body_str

def okx_get_balances() -> Tuple[int, Any]:
    path = "/api/v5/account/balance"
    url = OKX_API_BASE + path
    headers, _ = okx_headers("GET", path, None)
    r = requests.get(url, headers=headers, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"text": r.text}

def okx_withdraw(ccy: str, amt: str, to_addr: str, chain: str = "TRC20"):
    path = "/api/v5/asset/withdrawal"
    url = OKX_API_BASE + path
    body = {
        "ccy": ccy,
        "amt": amt,
        "dest": "4",    # 4 = to digital address
        "toAddr": to_addr,
        "chain": chain
    }
    headers, body_str = okx_headers("POST", path, body)
    r = requests.post(url, headers=headers, data=body_str, timeout=25)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"text": r.text}

# ------------------ Helpers ------------------
def safe_decimal(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError):
        return None

def find_usdt_balance(parsed_json) -> Optional[Decimal]:
    if not isinstance(parsed_json, dict):
        return None
    data = parsed_json.get("data") or parsed_json.get("balances") or parsed_json.get("result") or []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("ccy", "").upper() == "USDT":
                details = entry.get("details")
                if isinstance(details, list) and details:
                    for d in details:
                        val = d.get("availBal") or d.get("availEq") or d.get("cashBal") or d.get("balance")
                        if val:
                            dec = safe_decimal(str(val))
                            if dec is not None:
                                return dec
                for key in ("availBal", "availEq", "cashBal", "balance", "balanceCcy"):
                    if entry.get(key) is not None:
                        dec = safe_decimal(str(entry.get(key)))
                        if dec is not None:
                            return dec
        # fallback deeper search
        for entry in data:
            if isinstance(entry, dict):
                for k, v in entry.items():
                    if isinstance(v, str) and "usdt" in k.lower():
                        dec = safe_decimal(v)
                        if dec is not None:
                            return dec
    try:
        first = parsed_json.get("data", [])[0]
        if first:
            det = first.get("details", [])
            if det and len(det) > 0:
                for d in det:
                    if d.get("ccy", "").upper() == "USDT" or d.get("ccy") is None:
                        val = d.get("availBal") or d.get("cashBal")
                        if val:
                            dec = safe_decimal(str(val))
                            if dec is not None:
                                return dec
    except Exception:
        pass
    return None

def snapshot_balances() -> Optional[dict]:
    code, res = okx_get_balances()
    if code != 200:
        return None
    return res

# ------------------ Telegram handlers ------------------
# Track user conversational state in context.user_data keys

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
         InlineKeyboardButton("🇨🇳 中文", callback_data="lang_zh")]
    ])
    await update.message.reply_text(get_msg("welcome", "en"), reply_markup=kb)

async def lang_choice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = "en" if q.data == "lang_en" else "zh"
    uid = q.from_user.id
    c = DB.cursor()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, lang) VALUES (?, ?)", (uid, lang))
    c.execute("UPDATE users SET lang = ? WHERE telegram_id = ?", (lang, uid))
    DB.commit()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Set Wallet / 设置钱包", callback_data="set_wallet"),
         InlineKeyboardButton("💼 Start Escrow / 创建托管", callback_data="start_escrow")],
        [InlineKeyboardButton("📜 My Escrows / 我的托管", callback_data="list_escrows"),
         InlineKeyboardButton("💳 Check Deposit Balance / 查看余额", callback_data="check_balances")]
    ])
    await q.edit_message_text(get_msg("menu", lang), reply_markup=kb)

async def set_wallet_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    context.user_data['awaiting_wallet'] = True
    lang = get_user_lang(uid)
    await q.message.reply_text(get_msg("setwallet_prompt", lang), reply_markup=ReplyKeyboardRemove())

def get_user_lang(tg_id: int) -> str:
    c = DB.cursor()
    c.execute("SELECT lang FROM users WHERE telegram_id = ?", (tg_id,))
    r = c.fetchone()
    return r[0] if r else "en"

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.message.from_user.id
    lang = get_user_lang(uid)
    if context.user_data.get('awaiting_wallet'):
        wallet = text
        c = DB.cursor()
        c.execute("INSERT OR IGNORE INTO users (telegram_id, lang) VALUES (?, ?)", (uid, lang))
        c.execute("UPDATE users SET wallet = ? WHERE telegram_id = ?", (wallet, uid))
        DB.commit()
        context.user_data['awaiting_wallet'] = False
        await update.message.reply_text(get_msg("wallet_saved", lang))
        return

    if context.user_data.get('awaiting_amount'):
        amt_str = text
        dec = safe_decimal(amt_str)
        if dec is None or dec <= 0:
            await update.message.reply_text(get_msg("invalid_amount", lang))
            return
        snapshot = snapshot_balances()
        c = DB.cursor()
        c.execute("""
            INSERT INTO escrows (seeker_id, provider_wallet, amount, status, deposit_snapshot)
            VALUES (?, ?, ?, ?, ?)
        """, (uid, None, str(dec), "created", json.dumps(snapshot) if snapshot else None))
        DB.commit()
        escrow_id = c.lastrowid
        context.user_data['awaiting_amount'] = False
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy Wallet Address / 复制地址", callback_data=f"copyaddr_{escrow_id}"),
             InlineKeyboardButton("✅ Mark as Paid / 标记为已支付", callback_data=f"markpaid_{escrow_id}")],
            [InlineKeyboardButton("❌ Cancel / 取消", callback_data=f"cancel_{escrow_id}")]
        ])
        await update.message.reply_text(get_msg("deposit_info", lang, addr=DEPOSIT_ADDRESS), reply_markup=kb)
        try:
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"New escrow #{escrow_id} created for {dec} USDT by @{update.message.from_user.username or update.message.from_user.id}")
        except Exception:
            logger.exception("failed to notify admin")
        return

    await update.message.reply_text("Use /start to begin / 使用 /start 开始")

async def start_escrow_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = get_user_lang(uid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 I am Service Seeker / 我是付款方", callback_data="role_seeker"),
         InlineKeyboardButton("🧑‍💼 I am Service Provider / 我是收款方", callback_data="role_provider")]
    ])
    await q.edit_message_text("Select role / 请选择角色", reply_markup=kb)

async def role_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = get_user_lang(uid)
    if q.data == "role_provider":
        context.user_data['awaiting_wallet'] = True
        await q.edit_message_text(get_msg("setwallet_prompt", lang))
    else:
        context.user_data['awaiting_amount'] = True
        await q.edit_message_text(get_msg("enter_amount", lang))

async def copyaddr_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Address shown above. Copy it manually on mobile / 地址已显示，请手动复制。")

async def markpaid_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        escrow_id = int(q.data.split("_")[1])
    except:
        await q.edit_message_text("Invalid escrow id")
        return
    c = DB.cursor()
    c.execute("SELECT status, amount, seeker_id FROM escrows WHERE id = ?", (escrow_id,))
    row = c.fetchone()
    if not row:
        await q.edit_message_text("Escrow not found.")
        return
    status, amount_str, seeker_id = row
    if status not in ("created", "paid"):
        await q.edit_message_text("Escrow already processed.")
        return
    c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("paid", escrow_id))
    DB.commit()
    lang = get_user_lang(q.from_user.id)
    await q.edit_message_text(get_msg("paid_marked", lang))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Payment", callback_data=f"admin_confirm_{escrow_id}"),
         InlineKeyboardButton("❌ Cancel Escrow", callback_data=f"admin_cancel_{escrow_id}")]
    ])
    try:
        await context.bot.send_message(ADMIN_TELEGRAM_ID, f"Escrow #{escrow_id} marked PAID by user {seeker_id}. Please confirm (auto-check available).", reply_markup=kb)
    except Exception:
        logger.exception("failed to notify admin")

# cancel callback (simple)
async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        escrow_id = int(q.data.split("_")[1])
    except:
        await q.edit_message_text("Invalid id")
        return
    c = DB.cursor()
    c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("cancelled", escrow_id))
    DB.commit()
    lang = get_user_lang(q.from_user.id)
    await q.edit_message_text(get_msg("cancelled", lang))
    try:
        c2 = DB.cursor()
        c2.execute("SELECT seeker_id FROM escrows WHERE id = ?", (escrow_id,))
        r = c2.fetchone()
        if r:
            seeker_id = r[0]
            await context.bot.send_message(seeker_id, f"Escrow #{escrow_id} has been cancelled.")
    except Exception:
        pass

# Admin inline handler
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if uid != ADMIN_TELEGRAM_ID:
        await q.edit_message_text(get_msg("only_admin", get_user_lang(uid)))
        return
    parts = q.data.split("_")
    if len(parts) < 3:
        await q.edit_message_text("Invalid action")
        return
    action = parts[1]
    try:
        escrow_id = int(parts[2])
    except:
        await q.edit_message_text("Invalid escrow id")
        return
    c = DB.cursor()
    c.execute("SELECT status, amount, deposit_snapshot, seeker_id, provider_wallet FROM escrows WHERE id = ?", (escrow_id,))
    row = c.fetchone()
    if not row:
        await q.edit_message_text("Escrow not found.")
        return
    status, amount_str, deposit_snapshot_json, seeker_id, provider_wallet = row
    lang = get_user_lang(uid)

    if action == "confirm":
        requested = safe_decimal(amount_str) or Decimal("0")
        code, parsed = okx_get_balances()
        if code != 200:
            c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("confirmed", escrow_id))
            DB.commit()
            await q.edit_message_text(get_msg("confirmed", "en") + " / " + get_msg("confirmed", "zh") + "\n(Note: OKX balance check failed; confirmed manually.)")
            return
        current_usdt = find_usdt_balance(parsed) or Decimal("0")
        previous_usdt = None
        try:
            previous = json.loads(deposit_snapshot_json) if deposit_snapshot_json else None
            previous_usdt = find_usdt_balance(previous) if previous else None
        except Exception:
            previous_usdt = None

        if previous_usdt is not None:
            if current_usdt >= (previous_usdt + requested):
                c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("confirmed", escrow_id))
                DB.commit()
                await q.edit_message_text(get_msg("confirmed", "en") + " / " + get_msg("confirmed", "zh") + "\nAuto-check: incoming funds detected.")
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Release Funds", callback_data=f"admin_release_{escrow_id}")]])
                await context.bot.send_message(ADMIN_TELEGRAM_ID, f"Escrow #{escrow_id} confirmed and funds detected. Release when ready.", reply_markup=kb)
                return
            else:
                await q.edit_message_text("OKX auto-check: no incoming funds detected yet. You may still confirm manually or wait.")
                return
        else:
            c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("confirmed", escrow_id))
            DB.commit()
            await q.edit_message_text(get_msg("confirmed", "en") + " / " + get_msg("confirmed", "zh") + "\n(Note: no previous snapshot to auto-check.)")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Release Funds", callback_data=f"admin_release_{escrow_id}")]])
            await context.bot.send_message(ADMIN_TELEGRAM_ID, f"Escrow #{escrow_id} confirmed manually. Release when ready.", reply_markup=kb)
            return

    elif action == "cancel":
        c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("cancelled", escrow_id))
        DB.commit()
        await q.edit_message_text(get_msg("cancelled", "en") + " / " + get_msg("cancelled", "zh"))
        try:
            await context.bot.send_message(seeker_id, f"Escrow #{escrow_id} has been cancelled by admin.")
        except:
            pass
        return

    elif action == "release":
        if not provider_wallet:
            await q.edit_message_text("Provider wallet not known. Use /release <id> <wallet> command as admin.")
            return
        amt = amount_str
        code, res = okx_withdraw("USDT", amt, provider_wallet, chain="TRC20")
        if code == 200 and (res.get("code") in (None, "0")):
            txid = None
            data_field = res.get("data")
            if isinstance(data_field, list) and len(data_field) > 0:
                txid = data_field[0].get("wdId")
            c.execute("UPDATE escrows SET status = ?, okx_tx_id = ? WHERE id = ?", ("released", txid or "manual", escrow_id))
            DB.commit()
            await q.edit_message_text(get_msg("released", "en") + " / " + get_msg("released", "zh"))
            try:
                await context.bot.send_message(seeker_id, f"Escrow #{escrow_id} released. TX: {txid or 'manual'}")
            except:
                pass
            return
        else:
            await q.edit_message_text(get_msg("okx_withdraw_failed", "en", resp=str(res)) + "\n" + get_msg("okx_withdraw_failed", "zh", resp=str(res)))
            return

# Admin text commands
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return await update.message.reply_text(get_msg("only_admin", get_user_lang(user.id)))
    if not context.args:
        return await update.message.reply_text("Usage: /confirm <escrow_id>")
    try:
        escrow_id = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid id")
    c = DB.cursor()
    c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("confirmed", escrow_id))
    DB.commit()
    await update.message.reply_text(get_msg("confirmed", "en") + " / " + get_msg("confirmed", "zh"))

async def cmd_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return await update.message.reply_text(get_msg("only_admin", get_user_lang(user.id)))
    if len(context.args) < 1:
        return await update.message.reply_text("Usage: /release <escrow_id> [provider_wallet]")
    try:
        escrow_id = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid escrow id")
    provider_wallet = context.args[1] if len(context.args) > 1 else None
    c = DB.cursor()
    c.execute("SELECT amount, provider_wallet, seeker_id FROM escrows WHERE id = ?", (escrow_id,))
    row = c.fetchone()
    if not row:
        return await update.message.reply_text("Escrow not found.")
    amount_str, stored_wallet, seeker_id = row
    target_wallet = provider_wallet or stored_wallet
    if not target_wallet:
        return await update.message.reply_text("No provider wallet known. Provide wallet: /release <id> <wallet>")
    code, res = okx_withdraw("USDT", str(amount_str), target_wallet, chain="TRC20")
    if code == 200 and (res.get("code") in (None, "0")):
        txid = None
        df = res.get("data")
        if isinstance(df, list) and df:
            txid = df[0].get("wdId")
        c.execute("UPDATE escrows SET status = ?, okx_tx_id = ?, provider_wallet = ? WHERE id = ?", ("released", txid or "manual", target_wallet, escrow_id))
        DB.commit()
        await update.message.reply_text(get_msg("released", "en") + " / " + get_msg("released", "zh"))
        try:
            await context.bot.send_message(seeker_id, f"Escrow #{escrow_id} released. TX: {txid or 'manual'}")
        except:
            pass
    else:
        await update.message.reply_text(get_msg("okx_withdraw_failed", "en", resp=str(res)))

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return await update.message.reply_text(get_msg("only_admin", get_user_lang(user.id)))
    if not context.args:
        return await update.message.reply_text("Usage: /cancel <escrow_id>")
    try:
        escrow_id = int(context.args[0])
    except:
        return await update.message.reply_text("Invalid id")
    c = DB.cursor()
    c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("cancelled", escrow_id))
    DB.commit()
    await update.message.reply_text(get_msg("cancelled", "en") + " / " + get_msg("cancelled", "zh"))

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_user_lang(uid)
    c = DB.cursor()
    c.execute("SELECT id, seeker_id, provider_wallet, amount, status, created_at FROM escrows ORDER BY created_at DESC LIMIT 50")
    rows = c.fetchall()
    if not rows:
        return await update.message.reply_text(get_msg("list_empty", lang))
    lines = []
    for r in rows:
        lines.append(f"#{r[0]} | amt={r[3]} | status={r[4]} | provider_wallet={r[2]} | seeker={r[1]} | at={r[5]}")
    for i in range(0, len(lines), 20):
        await update.message.reply_text("\n".join(lines[i:i+20]))

async def cmd_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_TELEGRAM_ID:
        return await update.message.reply_text(get_msg("only_admin", get_user_lang(uid)))
    code, parsed = okx_get_balances()
    if code != 200:
        return await update.message.reply_text(f"OKX error: HTTP {code} {parsed}")
    await update.message.reply_text("OKX balances (raw):\n" + json.dumps(parsed, indent=2)[:3900])

# ------------------ Register & start ------------------
def register_handlers(app):
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(lang_choice_cb, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(set_wallet_cb, pattern="^set_wallet$"))
    app.add_handler(CallbackQueryHandler(start_escrow_cb, pattern="^start_escrow$"))
    app.add_handler(CallbackQueryHandler(role_cb, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(copyaddr_cb, pattern="^copyaddr_"))
    app.add_handler(CallbackQueryHandler(markpaid_cb, pattern="^markpaid_"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern="^cancel_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("release", cmd_release))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("escrows", cmd_list))
    app.add_handler(CommandHandler("balances", cmd_balances))

# Create application
if not BOT_TOKEN:
    logger.error("BOT_TOKEN missing in environment.")
    raise SystemExit("BOT_TOKEN not set")

application = ApplicationBuilder().token(BOT_TOKEN).build()
register_handlers(application)

# Start the Application in background asyncio loop so it can process update_queue
async def start_app_background(app):
    await app.initialize()
    await app.start()
    logger.info("Telegram Application initialized & started (background).")

# Create Flask app for webhook
flask_app = Flask(__name__)

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram will POST updates here"""
    payload = request.get_json(force=True)
    if not payload:
        return "no payload", 400
    try:
        update = Update.de_json(payload, application.bot)
        # put update into application queue for processing by handlers
        application.update_queue.put_nowait(update)
    except Exception as e:
        logger.exception("Failed to enqueue update: %s", e)
        return "error", 500
    return "ok", 200

@flask_app.route("/", methods=["GET"])
def index():
    return "Escrow Shield running.", 200

# Kick off background loop when module loaded (Vercel will import and run)
def bootstrap_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(start_app_background(application))
    # keep loop running in background in separate thread
    import threading
    def run_loop():
        try:
            loop.run_forever()
        except Exception:
            logger.exception("Async loop ended")
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    logger.info("Background asyncio loop started in thread.")

# Start the background app
bootstrap_async_loop()

# Run the Flask app using waitress when run locally; Vercel will use gunicorn/its runtime
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logger.info("Starting Flask (waitress) on port %s", port)
    serve(flask_app, host="0.0.0.0", port=port)
