# Setup

## Requirements

Use Python 3.12+

## Create and activate a virtual environment

    bash
    cd /root
    git clone 
    python3.12 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
## Create and activate a virtual environment
Create VK Group

Enable Messages for Group (Настройки, сообщения, сообщения сообщества) https://vk.ru/clubID/settings/messages

#Enable Bot Capabilities (for inline_keyboard) (Настройки, сообщения, настройки для бота, возможности  ботов https://vk.ru/clubID/settings/bots

Enable Long Poll API (Настройки, дополнительно, работа с API, Long Poll API) https://vk.ru/clubID?act=longpoll_api

Setup access_token (Настройки, дополнительно, работа с API), доступы: сообщения соообщества, управление сообществом) https://vk.ru/clubID?act=tokens 



## Set TOKEN AND VK_USER_ID in config/bot_config.txt
Set TOKEN and VK_USER_ID in config/bot_config.txt.
Check the other files in `config/` for additional settings.

    config/bot_config.txt.example -> file with dummy token and vk_user_id, rename to bot_config.txt
    config/entrypoint.txt -> where/how the bash session starts: workdir=<dir>, init=<command>
    config/delay_config.txt -> delay in seconds in when using interactive commands to avoid spamming VK_API with too many messages (default 5.0)
    config/interactive_list.txt -> list of commands treated as interactive (legacy behavior)
    config/keyboard_enable.txt -> true/false for inline_keyboard in VK (true by default)
    config/keyboard_list.txt -> list of commands in inline_keyboard
    requirements.txt -> python dependencies
    config/version.txt -> version of bot for debug purposes

##   Run the bot manually (for testing purposes)
	source venv/bin/activate
	python3 run.py

##   Run the bot with systemd
Create the service file:

    sudo nano /etc/systemd/system/vk-interactive-bot.service

 An example service file is available in the project’s root directory, name: vk-interactive-bot.service
Then reload systemd, enable the service, and start it:

    sudo systemctl daemon-reload
    sudo systemctl enable vk-interactive-bot
    sudo systemctl start vk-interactive-bot

## Available commands (in VK chat)

- normal command — runs in a live PTY bash session
- `art: <command>` — run in the active session and send result as an image (colors!)
  - `art:bg dark|light|auto` — image background; `art:font N` — font size
  - `art:last` — resend the last ART image
  - `art:doc <command>` — send result as a document (PNG) instead of a photo
- `bg: <command>` — run in background; output arrives in chat as it is produced
- `reset` / `сброс` / `exit` — reset the session
- `💀 Hard Reset` — kill a stuck session (new one is created on the next command)
- `ctrl` / `ctrl+c` — send SIGINT to interrupt output
- `status` / `статус` — bot/session/server diagnostics
- `🖼 ART` — all commands as images; `📄 Текст` — back to text
- `help` / `помощь` / `?` — this list
