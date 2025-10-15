#!/usr/bin/env python3
"""
Escrow Shield - Group-Optimized Telegram Escrow Bot
- Enhanced for group chat usage between two parties
- Inline keyboard UI for all interactions (English + 简体中文)
- Admin oversight for confirmations & releases
- OKX v5 API integration for automated withdrawals
- SQLite storage for escrows and users
"""

import os
import time
import hmac
import hashlib
import base64
import json
import sqlite3
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Any

import requests
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_ID") or 0)
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
DEPOSIT_ADDRESS = os.getenv("DEPOSIT_ADDRESS", "") or "Set_DEPOSIT_ADDRESS_IN_ENV"
SQLITE_FILE = os.getenv("SQLITE_FILE", "escrow_bot.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("escrow_bot")

def init_db(path: str = SQLITE_FILE):
    """Initialize database with improved schema for group escrows"""
    conn = sqlite3.connect(path, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY,
      telegram_id INTEGER UNIQUE,
      username TEXT,
      lang TEXT DEFAULT 'en',
      wallet TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS escrows (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER,
      buyer_id INTEGER,
      seller_id INTEGER,
      seller_wallet TEXT,
      amount TEXT,
      currency TEXT DEFAULT 'USDT',
      description TEXT,
      status TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      paid_at DATETIME,
      confirmed_at DATETIME,
      released_at DATETIME,
      okx_tx_id TEXT,
      deposit_snapshot TEXT
    )""")
    conn.commit()
    return conn

DB = init_db(SQLITE_FILE)

MESSAGES = {
    "welcome": {"en": "🛡️ Welcome to Escrow Shield!\n\nSecure escrow service for group transactions.\n\nUse /newescrow to start", 
                "zh": "🛡️ 欢迎使用 Escrow Shield！\n\n为群组交易提供安全托管服务。\n\n使用 /newescrow 开始"},
    "group_only": {"en": "⚠️ This bot is designed for group chats. Please add it to a group.", 
                   "zh": "⚠️ 此机器人专为群组聊天设计。请将其添加到群组中。"},
    "enter_amount": {"en": "💵 Enter escrow amount in USDT:\n<i>Example: 100 or 50.5</i>", 
                     "zh": "💵 输入托管金额（USDT）：\n<i>例如：100 或 50.5</i>"},
    "invalid_amount": {"en": "❌ Invalid amount. Enter a number (e.g. 10 or 10.5).", 
                       "zh": "❌ 无效金额。请输入数字（例如 10 或 10.5）。"},
    "enter_description": {"en": "📝 Enter brief description:\n<i>Example: Website development</i>", 
                          "zh": "📝 输入简短描述：\n<i>例如：网站开发</i>"},
    "escrow_created": {"en": "✅ Escrow #{id} created!\n\n💵 Amount: {amt} USDT\n📝 Description: {desc}\n\n👉 Seller should click button below to set wallet.", 
                       "zh": "✅ 托管 #{id} 已创建！\n\n💵 金额：{amt} USDT\n📝 描述：{desc}\n\n👉 卖家请点击下方按钮设置钱包。"},
    "seller_prompt": {"en": "🧑‍💼 Setting up as Seller\n\nSend your TRC20 (USDT) wallet address:", 
                      "zh": "🧑‍💼 设置为卖家\n\n请发送您的 TRC20 (USDT) 钱包地址："},
    "wallet_saved": {"en": "✅ Seller wallet saved!\n\n👉 Buyer can now view payment address.", 
                     "zh": "✅ 卖家钱包已保存！\n\n👉 买家现在可以查看付款地址。"},
    "payment_address": {"en": "💳 Payment Address\n\nSend USDT (TRC20) to:\n\n<code>{addr}</code>\n\nAfter sending, click 'Mark as Paid'", 
                        "zh": "💳 付款地址\n\n请将 USDT (TRC20) 发送至：\n\n<code>{addr}</code>\n\n发送后，点击「标记为已支付」"},
    "paid_marked": {"en": "✅ Marked as paid!\n\nWaiting for admin to confirm...", 
                    "zh": "✅ 已标记为已支付！\n\n等待管理员确认..."},
    "confirmed": {"en": "✅ Payment confirmed!", "zh": "✅ 付款已确认！"},
    "seller_deliver": {"en": "📦 Please deliver goods/services and click 'Confirm Delivery'", 
                       "zh": "📦 请交付商品/服务并点击「确认交付」"},
    "delivered": {"en": "📦 Delivery confirmed!\n\nWaiting for admin to release funds...", 
                  "zh": "📦 交付已确认！\n\n等待管理员释放资金..."},
    "released": {"en": "🎉 Funds released!\n\nEscrow complete. Thank you!", 
                 "zh": "🎉 资金已释放！\n\n托管完成。谢谢！"},
    "cancelled": {"en": "❌ Escrow cancelled.", "zh": "❌ 托管已取消。"},
    "only_admin": {"en": "⚠️ Only admin can do that.", "zh": "⚠️ 只有管理员可以执行。"},
    "only_buyer": {"en": "⚠️ Only buyer can do that.", "zh": "⚠️ 只有买家可以执行。"},
    "only_seller": {"en": "⚠️ Only seller can do that.", "zh": "⚠️ 只有卖家可以执行。"},
    "okx_withdraw_failed": {"en": "❌ OKX withdraw failed: {resp}", "zh": "❌ OKX 提现失败：{resp}"},
}

def get_msg(key: str, lang: str = "en", **kwargs) -> str:
    t = MESSAGES.get(key, {}).get(lang) or MESSAGES.get(key, {}).get("en") or ""
    return t.format(**kwargs) if kwargs else t

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
    try:
        r = requests.get(url, headers=headers, timeout=15)
        return r.status_code, r.json()
    except Exception as e:
        logger.error(f"OKX balance error: {e}")
        return 500, {"error": str(e)}

def okx_withdraw(ccy: str, amt: str, to_addr: str, chain: str = "TRC20"):
    path = "/api/v5/asset/withdrawal"
    url = OKX_API_BASE + path
    body = {
        "ccy": ccy,
        "amt": amt,
        "dest": "4",
        "toAddr": to_addr,
        "chain": chain
    }
    headers, body_str = okx_headers("POST", path, body)
    try:
        r = requests.post(url, headers=headers, data=body_str, timeout=25)
        return r.status_code, r.json()
    except Exception as e:
        logger.error(f"OKX withdraw error: {e}")
        return 500, {"error": str(e)}

def safe_decimal(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError, ValueError):
        return None

def find_usdt_balance(parsed_json) -> Optional[Decimal]:
    if not isinstance(parsed_json, dict):
        return None
    data = parsed_json.get("data") or []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                details = entry.get("details", [])
                if isinstance(details, list):
                    for d in details:
                        if d.get("ccy", "").upper() == "USDT":
                            val = d.get("availBal") or d.get("cashBal")
                            if val:
                                return safe_decimal(str(val))
    return None

def snapshot_balances() -> Optional[dict]:
    code, res = okx_get_balances()
    return res if code == 200 else None

def get_user_lang(tg_id: int) -> str:
    c = DB.cursor()
    c.execute("SELECT lang FROM users WHERE telegram_id = ?", (tg_id,))
    r = c.fetchone()
    return r[0] if r else "en"

def get_escrow_status_text(escrow_id: int, lang: str = "en") -> str:
    """Get formatted escrow status for display"""
    c = DB.cursor()
    c.execute("""
        SELECT chat_id, buyer_id, seller_id, seller_wallet, amount, currency, 
               description, status, created_at
        FROM escrows WHERE id = ?
    """, (escrow_id,))
    row = c.fetchone()
    
    if not row:
        return "❌ Escrow not found"
    
    chat_id, buyer_id, seller_id, seller_wallet, amount, currency, description, status, created_at = row
    
    status_emoji = {"created": "🆕", "paid": "💰", "confirmed": "✅", "released": "🎉", "cancelled": "❌"}.get(status, "📋")
    
    msg = f"{status_emoji} <b>Escrow #{escrow_id}</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"💵 Amount: <b>{amount} {currency}</b>\n"
    msg += f"👤 Buyer: <a href='tg://user?id={buyer_id}'>User {buyer_id}</a>\n"
    
    if seller_id:
        msg += f"🧑‍💼 Seller: <a href='tg://user?id={seller_id}'>User {seller_id}</a>\n"
    else:
        msg += f"🧑‍💼 Seller: <i>Not set</i>\n"
    
    if description:
        msg += f"📝 {description}\n"
    
    msg += f"📊 Status: <b>{status.upper()}</b>\n"
    
    return msg

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    lang = get_user_lang(update.effective_user.id)
    await update.message.reply_text(get_msg("welcome", lang), parse_mode='HTML')

async def newescrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /newescrow command - create new escrow in group"""
    if update.message.chat.type not in ['group', 'supergroup']:
        lang = get_user_lang(update.effective_user.id)
        await update.message.reply_text(get_msg("group_only", lang))
        return
    
    buyer_id = update.message.from_user.id
    buyer_username = update.message.from_user.username or f"User{buyer_id}"
    lang = get_user_lang(buyer_id)
    
    c = DB.cursor()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, lang) VALUES (?, ?, ?)", 
              (buyer_id, buyer_username, lang))
    DB.commit()
    
    context.user_data['creating_escrow'] = {
        'buyer_id': buyer_id,
        'chat_id': update.message.chat_id,
        'step': 'amount'
    }
    
    await update.message.reply_text(get_msg("enter_amount", lang), parse_mode='HTML')

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages during escrow creation and wallet setup"""
    text = update.message.text.strip()
    uid = update.message.from_user.id
    lang = get_user_lang(uid)
    
    if context.user_data.get('setting_wallet'):
        escrow_id = context.user_data['setting_wallet']['escrow_id']
        seller_id = context.user_data['setting_wallet']['seller_id']
        wallet = text
        
        if len(wallet) < 20 or len(wallet) > 50:
            await update.message.reply_text("❌ Invalid wallet format. Try again.")
            return
        
        c = DB.cursor()
        c.execute("UPDATE escrows SET seller_id = ?, seller_wallet = ? WHERE id = ?", 
                  (seller_id, wallet, escrow_id))
        c.execute("UPDATE users SET wallet = ? WHERE telegram_id = ?", 
                  (wallet, seller_id))
        DB.commit()
        
        del context.user_data['setting_wallet']
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 View Payment Address / 查看付款地址", callback_data=f"payaddr_{escrow_id}")],
            [InlineKeyboardButton("📋 View Details / 查看详情", callback_data=f"view_{escrow_id}")]
        ])
        
        await update.message.reply_text(
            get_msg("wallet_saved", lang),
            parse_mode='HTML',
            reply_markup=keyboard
        )
        return
    
    if context.user_data.get('creating_escrow'):
        escrow_data = context.user_data['creating_escrow']
        step = escrow_data.get('step')
        
        if step == 'amount':
            amount = safe_decimal(text)
            if not amount or amount <= 0:
                await update.message.reply_text(get_msg("invalid_amount", lang))
                return
            
            escrow_data['amount'] = str(amount)
            escrow_data['step'] = 'description'
            await update.message.reply_text(get_msg("enter_description", lang), parse_mode='HTML')
        
        elif step == 'description':
            description = text[:200]
            snapshot = snapshot_balances()
            
            c = DB.cursor()
            c.execute("""
                INSERT INTO escrows (chat_id, buyer_id, amount, description, status, deposit_snapshot)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                escrow_data['chat_id'],
                escrow_data['buyer_id'],
                escrow_data['amount'],
                description,
                'created',
                json.dumps(snapshot) if snapshot else None
            ))
            DB.commit()
            
            escrow_id = c.lastrowid
            del context.user_data['creating_escrow']
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧑‍💼 I'm the Seller / 我是卖家", callback_data=f"setseller_{escrow_id}")],
                [InlineKeyboardButton("📋 View Details / 查看详情", callback_data=f"view_{escrow_id}")]
            ])
            
            await update.message.reply_text(
                get_msg("escrow_created", lang, id=escrow_id, amt=escrow_data['amount'], desc=description),
                parse_mode='HTML',
                reply_markup=keyboard
            )
            
            if ADMIN_TELEGRAM_ID:
                try:
                    await context.bot.send_message(
                        ADMIN_TELEGRAM_ID,
                        f"🆕 New escrow #{escrow_id}\n"
                        f"Amount: {escrow_data['amount']} USDT\n"
                        f"Description: {description}"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline button callbacks"""
    q = update.callback_query
    await q.answer()
    
    data = q.data
    user_id = q.from_user.id
    lang = get_user_lang(user_id)
    
    if data.startswith("setseller_"):
        escrow_id = int(data.split("_")[1])
        
        c = DB.cursor()
        c.execute("SELECT buyer_id, seller_id FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if not row:
            await q.edit_message_text("❌ Escrow not found.")
            return
        
        buyer_id, seller_id = row
        
        if user_id == buyer_id:
            await q.answer(get_msg("only_seller", lang), show_alert=True)
            return
        
        if seller_id:
            await q.answer("⚠️ Seller already set!", show_alert=True)
            return
        
        context.user_data['setting_wallet'] = {
            'escrow_id': escrow_id,
            'seller_id': user_id
        }
        
        await q.edit_message_text(get_msg("seller_prompt", lang), parse_mode='HTML')
    
    elif data.startswith("view_"):
        escrow_id = int(data.split("_")[1])
        msg = get_escrow_status_text(escrow_id, lang)
        
        c = DB.cursor()
        c.execute("SELECT status, buyer_id, seller_id FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if row:
            status, buyer_id, seller_id = row
            keyboard = []
            
            if status == "created" and seller_id and user_id == buyer_id:
                keyboard.append([InlineKeyboardButton("💳 Payment Address / 付款地址", callback_data=f"payaddr_{escrow_id}")])
                keyboard.append([InlineKeyboardButton("✅ Mark Paid / 标记已付", callback_data=f"markpaid_{escrow_id}")])
            
            elif status == "confirmed" and user_id == seller_id:
                keyboard.append([InlineKeyboardButton("📦 Confirm Delivery / 确认交付", callback_data=f"delivered_{escrow_id}")])
            
            keyboard.append([InlineKeyboardButton("🔄 Refresh / 刷新", callback_data=f"view_{escrow_id}")])
            
            if user_id in [buyer_id, ADMIN_TELEGRAM_ID]:
                keyboard.append([InlineKeyboardButton("❌ Cancel / 取消", callback_data=f"cancel_{escrow_id}")])
            
            await q.edit_message_text(
                msg,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
        else:
            await q.edit_message_text(msg, parse_mode='HTML')
    
    elif data.startswith("payaddr_"):
        escrow_id = int(data.split("_")[1])
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Mark as Paid / 标记已付", callback_data=f"markpaid_{escrow_id}")],
            [InlineKeyboardButton("🔙 Back / 返回", callback_data=f"view_{escrow_id}")]
        ])
        
        await q.edit_message_text(
            get_msg("payment_address", lang, addr=DEPOSIT_ADDRESS),
            parse_mode='HTML',
            reply_markup=keyboard
        )
    
    elif data.startswith("markpaid_"):
        escrow_id = int(data.split("_")[1])
        
        c = DB.cursor()
        c.execute("SELECT buyer_id, status, amount FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if not row:
            await q.edit_message_text("❌ Escrow not found.")
            return
        
        buyer_id, status, amount = row
        
        if user_id != buyer_id:
            await q.answer(get_msg("only_buyer", lang), show_alert=True)
            return
        
        if status != "created":
            await q.answer("⚠️ Already marked!", show_alert=True)
            return
        
        c.execute("UPDATE escrows SET status = ?, paid_at = CURRENT_TIMESTAMP WHERE id = ?", 
                  ("paid", escrow_id))
        DB.commit()
        
        await q.edit_message_text(get_msg("paid_marked", lang), parse_mode='HTML')
        
        if ADMIN_TELEGRAM_ID:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Payment", callback_data=f"admin_confirm_{escrow_id}")],
                [InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{escrow_id}")]
            ])
            
            try:
                await context.bot.send_message(
                    ADMIN_TELEGRAM_ID,
                    f"💰 Escrow #{escrow_id} marked PAID\n"
                    f"Amount: {amount} USDT\n\n"
                    f"Please verify and confirm.",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")
    
    elif data.startswith("admin_confirm_"):
        if user_id != ADMIN_TELEGRAM_ID:
            await q.answer(get_msg("only_admin", lang), show_alert=True)
            return
        
        escrow_id = int(data.split("_")[2])
        
        c = DB.cursor()
        c.execute("UPDATE escrows SET status = ?, confirmed_at = CURRENT_TIMESTAMP WHERE id = ?", 
                  ("confirmed", escrow_id))
        c.execute("SELECT chat_id FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        DB.commit()
        
        await q.edit_message_text(f"{get_msg('confirmed', lang)}\n\nEscrow #{escrow_id}")
        
        if row:
            chat_id = row[0]
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Confirm Delivery / 确认交付", callback_data=f"delivered_{escrow_id}")],
                [InlineKeyboardButton("📋 View / 查看", callback_data=f"view_{escrow_id}")]
            ])
            
            try:
                await context.bot.send_message(
                    chat_id,
                    f"{get_msg('confirmed', 'en')} / {get_msg('confirmed', 'zh')}\n\n"
                    f"Escrow #{escrow_id}\n\n"
                    f"{get_msg('seller_deliver', 'en')}\n{get_msg('seller_deliver', 'zh')}",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Failed to send to group: {e}")
    
    elif data.startswith("admin_reject_"):
        if user_id != ADMIN_TELEGRAM_ID:
            await q.answer(get_msg("only_admin", lang), show_alert=True)
            return
        
        escrow_id = int(data.split("_")[2])
        
        c = DB.cursor()
        c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("created", escrow_id))
        DB.commit()
        
        await q.edit_message_text(f"❌ Payment rejected for escrow #{escrow_id}")
    
    elif data.startswith("delivered_"):
        escrow_id = int(data.split("_")[1])
        
        c = DB.cursor()
        c.execute("SELECT seller_id, status, seller_wallet, amount FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if not row:
            await q.edit_message_text("❌ Escrow not found.")
            return
        
        seller_id, status, seller_wallet, amount = row
        
        if user_id != seller_id:
            await q.answer(get_msg("only_seller", lang), show_alert=True)
            return
        
        if status != "confirmed":
            await q.answer("⚠️ Not confirmed yet!", show_alert=True)
            return
        
        await q.edit_message_text(get_msg("delivered", lang), parse_mode='HTML')
        
        if ADMIN_TELEGRAM_ID:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💸 Release Funds", callback_data=f"admin_release_{escrow_id}")]
            ])
            
            try:
                await context.bot.send_message(
                    ADMIN_TELEGRAM_ID,
                    f"📦 Delivery confirmed for escrow #{escrow_id}\n\n"
                    f"Amount: {amount} USDT\n"
                    f"Wallet: {seller_wallet}\n\n"
                    f"Ready to release?",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")
    
    elif data.startswith("admin_release_"):
        if user_id != ADMIN_TELEGRAM_ID:
            await q.answer(get_msg("only_admin", lang), show_alert=True)
            return
        
        escrow_id = int(data.split("_")[2])
        
        c = DB.cursor()
        c.execute("SELECT seller_wallet, amount, chat_id FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if not row:
            await q.edit_message_text("❌ Escrow not found.")
            return
        
        seller_wallet, amount, chat_id = row
        
        if not seller_wallet:
            await q.edit_message_text("❌ No seller wallet!")
            return
        
        await q.edit_message_text("⏳ Processing withdrawal...")
        
        code, res = okx_withdraw("USDT", amount, seller_wallet, chain="TRC20")
        
        if code == 200 and res.get("code") in (None, "0"):
            txid = None
            data_field = res.get("data")
            if isinstance(data_field, list) and data_field:
                txid = data_field[0].get("wdId")
            
            c.execute("UPDATE escrows SET status = ?, okx_tx_id = ?, released_at = CURRENT_TIMESTAMP WHERE id = ?", 
                      ("released", txid or "manual", escrow_id))
            DB.commit()
            
            await q.edit_message_text(
                f"{get_msg('released', 'en')}\n\n"
                f"Escrow #{escrow_id}\n"
                f"TX: {txid or 'manual'}"
            )
            
            try:
                await context.bot.send_message(
                    chat_id,
                    f"{get_msg('released', 'en')} / {get_msg('released', 'zh')}\n\n"
                    f"Escrow #{escrow_id}\n"
                    f"TX ID: <code>{txid or 'manual'}</code>",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Failed to send to group: {e}")
        else:
            await q.edit_message_text(get_msg("okx_withdraw_failed", lang, resp=str(res)))
    
    elif data.startswith("cancel_"):
        escrow_id = int(data.split("_")[1])
        
        c = DB.cursor()
        c.execute("SELECT buyer_id, status, chat_id FROM escrows WHERE id = ?", (escrow_id,))
        row = c.fetchone()
        
        if not row:
            await q.edit_message_text("❌ Escrow not found.")
            return
        
        buyer_id, status, chat_id = row
        
        if user_id not in [buyer_id, ADMIN_TELEGRAM_ID]:
            await q.answer(get_msg("only_admin", lang), show_alert=True)
            return
        
        if status in ("released", "cancelled"):
            await q.answer("⚠️ Cannot cancel!", show_alert=True)
            return
        
        c.execute("UPDATE escrows SET status = ? WHERE id = ?", ("cancelled", escrow_id))
        DB.commit()
        
        await q.edit_message_text(get_msg("cancelled", lang))
        
        try:
            await context.bot.send_message(chat_id, get_msg("cancelled", "en") + " / " + get_msg("cancelled", "zh") + f"\n\nEscrow #{escrow_id}")
        except Exception as e:
            logger.error(f"Failed to send cancellation: {e}")

async def escrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View escrow details"""
    if not context.args:
        await update.message.reply_text("Usage: /escrow [id]")
        return
    
    try:
        escrow_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")
        return
    
    lang = get_user_lang(update.effective_user.id)
    msg = get_escrow_status_text(escrow_id, lang)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View Details / 查看详情", callback_data=f"view_{escrow_id}")]
    ])
    
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check OKX balance (admin only)"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text(get_msg("only_admin", get_user_lang(update.effective_user.id)))
        return
    
    code, res = okx_get_balances()
    
    if code == 200:
        usdt_balance = find_usdt_balance(res)
        msg = "💰 <b>OKX Balance</b>\n\n"
        if usdt_balance:
            msg += f"USDT: <b>{usdt_balance}</b>"
        else:
            msg += f"<code>{json.dumps(res, indent=2)[:500]}</code>"
        await update.message.reply_text(msg, parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ Failed: {res}")

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        return
    
    logger.info("Starting Escrow Shield Bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("newescrow", newescrow_cmd))
    app.add_handler(CommandHandler("escrow", escrow_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    logger.info("Bot running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main(