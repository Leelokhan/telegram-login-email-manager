#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot：通过和 bot 对话来触发"改绑登录邮箱"流程
--------------------------------------------------------
架构说明：
  - Bot 本身（BotFather token）只负责聊天交互，不具备账号操作能力。
  - 真正改邮箱是脚本内用 Telethon 以你自己的账号登录（MTProto），
    跟 bot 对话拿到的每一步输入（手机号/验证码/密码/新邮箱/邮箱验证码）
    喂给 Telethon 完成操作。
  - 只有 ALLOWED_IDS 里的用户可以使用 /rebind_email。
  - 不在名单里的人发 /request 可以发起申请，ADMIN_IDS 里的管理员
    会收到带"同意/拒绝"按钮的通知，同意后自动加入 ALLOWED_IDS
    （持久化到本地 JSON 文件，重启不丢）。
  - 用于登录的 Telethon session 只存在于内存（StringSession），
    整个流程结束或出错都会 disconnect + 清空，不落盘。

依赖安装：
    pip install "python-telegram-bot>=20,<22" telethon

使用：
    1. 找 @BotFather 创建一个 bot，拿到 BOT_TOKEN
    2. 去 https://my.telegram.org 拿到 TG_API_ID / TG_API_HASH
    3. 用 @userinfobot 或类似工具查到自己的数字 Telegram ID
    4. 配置环境变量（见文末 .env.example）
    5. python3 rebind_email_bot.py
    6. 已在允许名单里的人发 /rebind_email 开始流程；
       不在名单里的人发 /request 申请权限。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    CodeInvalidError,
    EmailInvalidError,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== 配置：强烈建议用环境变量传入，而不是写死在代码里 ======
BOT_TOKEN = os.environ.get("REBIND_BOT_TOKEN", "")

# 管理员ID列表，逗号分隔。只有管理员能审批 /request 申请。
ADMIN_IDS = {
    int(x) for x in os.environ.get("REBIND_ADMIN_IDS", "").split(",") if x.strip()
}

# 初始允许名单（种子），逗号分隔。之后通过审批加入的用户会持久化到
# ALLOWED_IDS_FILE，重启后会和这里的种子值合并。
_SEED_ALLOWED_IDS = {
    int(x) for x in os.environ.get("REBIND_ALLOWED_IDS", "").split(",") if x.strip()
}

ALLOWED_IDS_FILE = Path(
    os.environ.get("REBIND_ALLOWED_IDS_FILE", "allowed_users.json")
)

TG_API_ID = int(os.environ.get("REBIND_TG_API_ID", "0"))
TG_API_HASH = os.environ.get("REBIND_TG_API_HASH", "")
# ================================================================


def load_allowed_ids() -> set:
    ids = set(_SEED_ALLOWED_IDS) | set(ADMIN_IDS)
    if ALLOWED_IDS_FILE.exists():
        try:
            data = json.loads(ALLOWED_IDS_FILE.read_text())
            ids |= {int(x) for x in data}
        except Exception:
            logger.warning("读取 %s 失败，忽略。", ALLOWED_IDS_FILE)
    return ids


def save_allowed_ids(ids: set):
    try:
        ALLOWED_IDS_FILE.write_text(json.dumps(sorted(ids)))
    except Exception as e:
        logger.error("保存允许名单失败: %s", e)


ALLOWED_IDS = load_allowed_ids()
# 待审批请求: user_id -> {"name": str, "username": str}
PENDING_REQUESTS: dict = {}

# 会话状态
PHONE, CODE, PASSWORD, NEW_EMAIL, EMAIL_CODE = range(5)


def allowed_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None or update.effective_user.id not in ALLOWED_IDS:
            if update.message:
                await update.message.reply_text(
                    "你还没有使用权限。发 /request 向管理员申请。"
                )
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


async def cleanup(context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.pop("client", None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    context.user_data.clear()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = user.id in ADMIN_IDS
    is_allowed = user.id in ALLOWED_IDS

    lines = ["📌 Telegram 登录邮箱改绑 Bot 使用说明", ""]

    if not is_allowed:
        lines += [
            "你目前还没有使用权限。",
            "发送 /request 向管理员申请，管理员同意后即可使用。",
            "",
        ]
    else:
        lines += [
            "【可用命令】",
            "/rebind_email — 开始改绑登录邮箱流程",
            "/cancel — 中途取消当前流程",
            "",
            "【流程说明】",
            "1. 发送 /rebind_email",
            "2. 输入要操作账号的手机号（带国家码，如 +8613800000000）",
            "3. 输入 Telegram 发到该手机/账号的登录验证码",
            "4. 如果账号开启了两步验证，输入两步验证密码",
            "5. 输入要绑定的新登录邮箱地址",
            "6. 输入 Telegram 发到新邮箱的验证码",
            "7. 完成，回 Telegram App「隐私和安全」页面确认已生效",
            "",
            "⚠️ 这个流程会用到你账号的登录验证码/两步验证密码，"
            "请确认是在自己信任的环境里操作，不要把 bot 链接转发给不认识的人。",
            "",
        ]

    if is_admin:
        lines += [
            "【管理员命令】",
            "/listusers — 查看允许名单和待审批申请",
            "/removeuser <用户ID> — 移除某人的使用权限",
            "收到申请时会自动推送带按钮的审批消息，直接点击即可。",
        ]

    await update.message.reply_text("\n".join(lines))


@allowed_only
async def start_rebind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup(context)  # 保险起见先清一次旧状态
    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
    await client.connect()
    context.user_data["client"] = client
    await update.message.reply_text(
        "开始改绑登录邮箱流程。\n"
        "请输入要操作的账号手机号（带国家码，例如 +8613800000000）：\n"
        "随时可发 /cancel 取消。"
    )
    return PHONE


@allowed_only
async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    client = context.user_data["client"]
    context.user_data["phone"] = phone
    try:
        await client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(f"发送验证码失败: {e}")
        await cleanup(context)
        return ConversationHandler.END
    await update.message.reply_text("已发送登录验证码，请输入你收到的验证码：")
    return CODE


@allowed_only
async def got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data["client"]
    phone = context.user_data["phone"]

    # 收到验证码后尽快从聊天记录里删掉这条消息
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        await context.bot.send_message(
            update.effective_chat.id, "检测到已开启两步验证，请输入当前两步验证密码："
        )
        return PASSWORD
    except PhoneCodeInvalidError:
        await context.bot.send_message(
            update.effective_chat.id, "验证码错误，流程终止，请重新 /rebind_email。"
        )
        await cleanup(context)
        return ConversationHandler.END
    except Exception as e:
        await context.bot.send_message(update.effective_chat.id, f"登录出错: {e}")
        await cleanup(context)
        return ConversationHandler.END

    await context.bot.send_message(
        update.effective_chat.id, "登录成功！请输入要绑定的新登录邮箱："
    )
    return NEW_EMAIL


@allowed_only
async def got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    client = context.user_data["client"]

    # 密码消息尽快删除
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await client.sign_in(password=pwd)
    except PasswordHashInvalidError:
        await context.bot.send_message(
            update.effective_chat.id, "密码错误，请重新输入两步验证密码："
        )
        return PASSWORD
    except Exception as e:
        await context.bot.send_message(update.effective_chat.id, f"登录出错: {e}")
        await cleanup(context)
        return ConversationHandler.END

    await context.bot.send_message(
        update.effective_chat.id, "登录成功！请输入要绑定的新登录邮箱："
    )
    return NEW_EMAIL


@allowed_only
async def got_new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_email = update.message.text.strip()
    context.user_data["new_email"] = new_email
    client = context.user_data["client"]

    # 这里用的是 account.sendVerifyEmailCode，操作的是独立的
    # "登录邮箱"（Login Email）功能，跟两步验证密码/恢复邮箱是两回事，
    # 不需要用到密码。
    try:
        sent = await client(
            functions.account.SendVerifyEmailCodeRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                email=new_email,
            )
        )
    except EmailInvalidError:
        await update.message.reply_text("邮箱格式不对，请重新输入：")
        return NEW_EMAIL
    except Exception as e:
        await update.message.reply_text(f"发送邮箱验证码失败: {e}")
        await cleanup(context)
        return ConversationHandler.END

    await update.message.reply_text(
        f"已发送验证码到 {sent.email_pattern}，请输入收到的{sent.length}位验证码："
    )
    return EMAIL_CODE


@allowed_only
async def got_email_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data["client"]
    new_email = context.user_data.get("new_email")

    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        result = await client(
            functions.account.VerifyEmailRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                verification=types.EmailVerificationCode(code=code),
            )
        )
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ 登录邮箱绑定成功！新邮箱: {getattr(result, 'email', new_email)}",
        )
    except CodeInvalidError:
        await context.bot.send_message(
            update.effective_chat.id, "验证码错误，请重新输入："
        )
        return EMAIL_CODE
    except Exception as e:
        await context.bot.send_message(update.effective_chat.id, f"❌ 出错: {e}")
    finally:
        pass

    await cleanup(context)
    return ConversationHandler.END


@allowed_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消，相关状态已清空。")
    await cleanup(context)
    return ConversationHandler.END


# ---------------- 申请审批流程 ----------------

async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in ALLOWED_IDS:
        await update.message.reply_text("你已经有权限了，直接发 /rebind_email 即可。")
        return
    if user.id in PENDING_REQUESTS:
        await update.message.reply_text("你的申请正在等待管理员审批，请耐心等待。")
        return
    if not ADMIN_IDS:
        await update.message.reply_text("当前没有配置管理员，无法处理申请。")
        return

    PENDING_REQUESTS[user.id] = {
        "name": user.full_name,
        "username": user.username or "",
    }

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 同意", callback_data=f"approve:{user.id}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"deny:{user.id}"),
            ]
        ]
    )
    lines = [
        "收到使用申请：",
        f"姓名: {user.full_name}",
    ]
    if user.username:
        lines.append(f"用户名: @{user.username}")
    lines.append(f"ID: {user.id}")
    text = "\n".join(lines)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=keyboard)
        except Exception as e:
            logger.warning("通知管理员 %s 失败: %s", admin_id, e)

    await update.message.reply_text("申请已发送，等待管理员审批。")


async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user

    if admin.id not in ADMIN_IDS:
        await query.answer("你不是管理员。", show_alert=True)
        return

    action, uid_str = query.data.split(":", 1)
    uid = int(uid_str)

    info = PENDING_REQUESTS.pop(uid, None)
    if info is None:
        await query.answer("这个申请已经处理过了。", show_alert=True)
        return

    if action == "approve":
        ALLOWED_IDS.add(uid)
        save_allowed_ids(ALLOWED_IDS)
        await query.edit_message_text(
            query.message.text + f"\n\n✅ 已由 {admin.full_name} 批准。"
        )
        try:
            await context.bot.send_message(
                uid, "你的申请已通过，现在可以发 /rebind_email 使用了。"
            )
        except Exception:
            pass
    else:
        await query.edit_message_text(
            query.message.text + f"\n\n❌ 已由 {admin.full_name} 拒绝。"
        )
        try:
            await context.bot.send_message(uid, "很抱歉，你的申请被拒绝了。")
        except Exception:
            pass

    await query.answer()


async def admin_only_guard(update: Update) -> bool:
    if update.effective_user is None or update.effective_user.id not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("无权限，仅管理员可用此命令。")
        return False
    return True


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    ids = ", ".join(str(i) for i in sorted(ALLOWED_IDS)) or "（空）"
    pending = ", ".join(str(i) for i in PENDING_REQUESTS) or "（空）"
    await update.message.reply_text(f"允许名单: {ids}\n待审批: {pending}")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args:
        await update.message.reply_text("用法: /removeuser <用户ID>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("用户ID必须是数字。")
        return
    if uid in ADMIN_IDS:
        await update.message.reply_text("不能移除管理员。")
        return
    ALLOWED_IDS.discard(uid)
    save_allowed_ids(ALLOWED_IDS)
    await update.message.reply_text(f"已移除 {uid}。")


def main():
    if not (BOT_TOKEN and ADMIN_IDS and TG_API_ID and TG_API_HASH):
        raise SystemExit(
            "请先设置环境变量 REBIND_BOT_TOKEN / REBIND_ADMIN_IDS / "
            "REBIND_TG_API_ID / REBIND_TG_API_HASH"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    async def post_init(application: Application):
        await application.bot.set_my_commands(
            [
                ("help", "查看使用说明"),
                ("rebind_email", "开始改绑登录邮箱"),
                ("request", "申请使用权限"),
                ("cancel", "取消当前流程"),
            ]
        )

    app.post_init = post_init

    conv = ConversationHandler(
        entry_points=[CommandHandler("rebind_email", start_rebind)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
            NEW_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_new_email)],
            EMAIL_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_email_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler(["help", "start"], help_command))
    app.add_handler(CommandHandler("request", request_access))
    app.add_handler(CommandHandler("listusers", list_users))
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(
        CallbackQueryHandler(handle_approval, pattern=r"^(approve|deny):\d+$")
    )

    logger.info("Bot 启动，等待命令...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
