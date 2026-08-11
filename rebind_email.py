#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 登录邮箱手动改绑脚本
------------------------------------
用途：手动、一次性地重新绑定 Telegram 账号的登录/恢复邮箱。
特点：
  - 全程手动输入验证码，不做任何自动读信操作。
  - 使用内存 session（StringSession），从不写入磁盘，
    运行结束后不留任何 session 文件。
  - 只做这一件事，跑完就退出。

使用前准备：
  1. 打开 https://my.telegram.org -> API development tools
     用你自己的 Telegram 账号登录，创建一个 application，
     拿到 api_id 和 api_hash，填到下面 API_ID / API_HASH。
  2. 在 VPS 上安装依赖：
        python3 -m venv venv
        source venv/bin/activate
        pip install telethon
  3. 运行：
        python3 rebind_email.py
  4. 跑完之后（无论成功与否）可以直接删掉这个脚本所在目录，
     不会有残留的登录凭证。
"""

import asyncio
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    EmailUnconfirmedError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
)

# ====== 在这里填入你自己在 my.telegram.org 申请到的信息 ======
API_ID = 0            # 例如 12345678
API_HASH = ""         # 例如 "abcdef1234567890abcdef1234567890"
# =============================================================


async def main():
    if not API_ID or not API_HASH:
        print("请先在脚本里填好 API_ID 和 API_HASH（来自 my.telegram.org）。")
        return

    # 纯内存 session，不落盘
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        phone = input("手机号（带国家码，如 +8613800000000）: ").strip()
        await client.send_code_request(phone)

        pwd_2fa = None  # 记录2FA密码，避免重复问

        try:
            code = input("输入 Telegram 发来的登录验证码: ").strip()
            await client.sign_in(phone=phone, code=code)
        except PhoneCodeInvalidError:
            print("验证码错误，脚本退出，请重新运行。")
            return
        except SessionPasswordNeededError:
            # 账号开启了两步验证，需要密码才能登录
            pwd_2fa = getpass("检测到已开启两步验证，请输入当前两步验证密码: ")
            try:
                await client.sign_in(password=pwd_2fa)
            except PasswordHashInvalidError:
                print("两步验证密码错误，脚本退出。")
                return

        if not await client.is_user_authorized():
            print("登录未完成，脚本退出。")
            return

        print("登录成功。")

        new_email = input("输入要绑定的新登录邮箱: ").strip()

        def email_code_callback(length):
            return input(f"请输入刚发送到 {new_email} 的 {length} 位验证码: ").strip()

        if pwd_2fa is None:
            # 理论上不会走到这，因为你说账号已开启2FA；
            # 如果账号本来没有2FA密码，这里会尝试直接设置邮箱，
            # Telegram 通常要求先有密码才能绑定恢复邮箱。
            print("未检测到已有的两步验证密码，无法仅设置邮箱。")
            print("如果这个账号确实没有开启两步验证，请先在 Telegram App 里设置一个密码。")
            return

        try:
            ok = await client.edit_2fa(
                current_password=pwd_2fa,
                new_password=pwd_2fa,   # 密码保持不变，只改邮箱
                email=new_email,
                email_code_callback=email_code_callback,
            )
            print("✅ 登录邮箱绑定成功！" if ok else "❌ 绑定失败，请检查信息后重试。")
        except EmailUnconfirmedError:
            print("❌ 邮箱验证码未确认，绑定未完成。")
        except PasswordHashInvalidError:
            print("❌ 两步验证密码错误，绑定未完成。")
        except Exception as e:
            print(f"❌ 出错: {e}")

    finally:
        await client.disconnect()
        print("已断开连接，本次 session 未保存，退出。")


if __name__ == "__main__":
    asyncio.run(main())
