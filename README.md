# telegram-login-email-manager
必须用你自己的 Telegram 账号通过 Telethon（MTProto 客户端库）登录一次来操作。
不落盘：脚本用 StringSession()（纯内存 session），不调用 .save()，脚本运行结束、进程退出后，内存里的 session 数据就没了，硬盘上不会留下任何 .session 文件。
流程：手机号 → Telegram 发来的登录验证码 → 你的两步验证密码 → 输入新邮箱 → Telegram 发验证码到新邮箱 → 你手动输入 → 绑定完成。
改邮箱不改密码：技术上是"用同一个密码重新提交一次2FA设置，同时带上新邮箱"，所以过程中会用到你的2FA密码，但密码本身不会变。

部署到 VPS：

bash
mkdir -p ~/rebind-email-bot && cd ~/rebind-email-bot
# 把上面4个文件放进这个目录
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
nano .env   # 填入 BOT_TOKEN / OWNER_ID / TG_API_ID / TG_API_HASH

sudo cp rebind-email-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/rebind-email-bot.service  # 改成实际用户名和路径
sudo systemctl daemon-reload
sudo systemctl enable --now rebind-email-bot
sudo systemctl status rebind-email-bot

之后在 Telegram 里找到你创建的 bot，发 /rebind_email，跟着提示依次输入手机号、登录验证码、两步验证密码、新邮箱、邮箱验证码即可，随时可发 /cancel 中止。
