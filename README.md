# telegram-login-email-manager
用户账号登录，不是 BotFather 那种 bot：改登录邮箱是账号安全设置，Bot API 做不到，必须用你自己的 Telegram 账号通过 Telethon（MTProto 客户端库）登录一次来操作。
不落盘：脚本用 StringSession()（纯内存 session），不调用 .save()，脚本运行结束、进程退出后，内存里的 session 数据就没了，硬盘上不会留下任何 .session 文件。
流程：手机号 → Telegram 发来的登录验证码 → 你的两步验证密码 → 输入新邮箱 → Telegram 发验证码到新邮箱 → 你手动输入 → 绑定完成。
改邮箱不改密码：技术上是"用同一个密码重新提交一次2FA设置，同时带上新邮箱"，所以过程中会用到你的2FA密码，但密码本身不会变。
