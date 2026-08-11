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

    await context.bot.send_message(
        update.effective_chat.id, "登录成功！请输入要绑定的新登录邮箱："
    )
    return NEW_EMAIL


@owner_only
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


@owner_only
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
