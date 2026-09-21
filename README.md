python3.12


python3.12 -m venv venv

source venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt

nano /etc/systemd/system/vk-interactive-bot.service (example in root directory of project)

systemctl daemon-reload
systemctl enable vk-interactive-bot
sudo systemctl start vk-interactive-bot

SETUP TOKEN/VK_USER_ID in bot_config.txt


Check other txt files for other settings


python3 async_server_bot.py
