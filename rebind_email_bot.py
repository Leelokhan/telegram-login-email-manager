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
  - 只有 OWNER_ID 这个用户可以使用本 bot 的任何命令。
  - 用于登录的 Telethon session 只存在于内存（StringSession），
    整个流程结束或出错都会 disconnect + 清空，不落盘。

依赖安装：
    pip install "python-telegram-bot>=20,<22" telethon

使用：
    1. 找 @BotFather 创建一个 bot，拿到 BOT_TOKEN
    2. 去 https://my.telegram.org 拿到 TG_API_ID / TG_API_HASH
    3. 用 @userinfobot 或类似工具查到自己的数字 Telegram ID，填到 OWNER_ID
    4. 把这四项配置好（建议用环境变量而不是硬编码，见文末说明）
    5. python3 rebind_email_bot.py
    6. 在 Telegram 里找到你的 bot，发 /rebind_email 开始流程
"""

import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    EmailUnconfirmedError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== 配置：强烈建议用环境变量传入，而不是写死在代码里 ======
BOT_TOKEN = os.environ.get("REBIND_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("REBIND_OWNER_ID", "0"))
TG_API_ID = int(os.environ.get("REBIND_TG_API_ID", "0"))
TG_API_HASH = os.environ.get("REBIND_TG_API_HASH", "")
# ================================================================

# 会话状态
PHONE, CODE, PASSWORD, NEW_EMAIL, EMAIL_CODE = range(5)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None or update.effective_user.id != OWNER_ID:
            if update.message:
                await update.message.reply_text("无权限。")
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


@owner_only
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


@owner_only
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


@owner_only
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


@owner_only
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

    context.user_data["pwd_2fa"] = pwd
    await context.bot.send_message(
        update.effective_chat.id, "登录成功！请输入要绑定的新登录邮箱："
    )
    return NEW_EMAIL


@owner_only
async def got_new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_email = update.message.text.strip()
    context.user_data["new_email"] = new_email

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    context.user_data["email_code_future"] = fut

    async def email_code_callback(length):
        code = await fut
        return code

    client = context.user_data["client"]
    pwd_2fa = context.user_data.get("pwd_2fa")
    chat_id = update.effective_chat.id

    if pwd_2fa is None:
        await update.message.reply_text(
            "这个账号没有检测到两步验证密码，无法仅设置邮箱。"
            "请先在 Telegram App 内设置两步验证密码后再试。"
        )
        await cleanup(context)
        return ConversationHandler.END

    await update.message.reply_text(
        f"已请求向 {new_email} 发送验证码，收到后请把验证码发给我："
    )

    async def do_edit():
        try:
            ok = await client.edit_2fa(
                current_password=pwd_2fa,
                new_password=pwd_2fa,
                email=new_email,
                email_code_callback=email_code_callback,
            )
            await context.bot.send_message(
                chat_id, "✅ 登录邮箱绑定成功！" if ok else "❌ 绑定失败，请检查后重试。"
            )
        except EmailUnconfirmedError:
            await context.bot.send_message(chat_id, "❌ 邮箱验证码未确认，绑定未完成。")
        except Exception as e:
            await context.bot.send_message(chat_id, f"❌ 出错: {e}")
        finally:
            await cleanup(context)

    context.user_data["edit_task"] = asyncio.create_task(do_edit())
    return EMAIL_CODE


@owner_only
async def got_email_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    fut = context.user_data.get("email_code_future")
    if fut and not fut.done():
        fut.set_result(code)

    task = context.user_data.get("edit_task")
    if task:
        await task  # 等后台的 edit_2fa 跑完，结果会由 do_edit() 自己发消息通知
    return ConversationHandler.END


@owner_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("已取消，相关状态已清空。")
    await cleanup(context)
    return ConversationHandler.END


def main():
    if not (BOT_TOKEN and OWNER_ID and TG_API_ID and TG_API_HASH):
        raise SystemExit(
            "请先设置环境变量 REBIND_BOT_TOKEN / REBIND_OWNER_ID / "
            "REBIND_TG_API_ID / REBIND_TG_API_HASH"
        )

    app = Application.builder().token(BOT_TOKEN).build()

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

    logger.info("Bot 启动，等待 /rebind_email 命令...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
