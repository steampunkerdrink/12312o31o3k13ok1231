# VK Interactive CLI Bot

Shell over VK: run commands on a server through a VK community chat, with an optional ART mode
(command output rendered as an image).

## Requirements

- Python 3.12+
- Linux (uses PTY, `/bin/bash`). The bot does NOT run on Windows.

## Project structure

    run.py                  entry point: from bot.app import main
    bot/                    code
      app.py                bot: message handling, PTY sessions, routing, ART pipeline
      listener.py           PTY output listener, ANSI cleanup, end-marker, flood control
      render.py             ANSI/SGR -> PNG renderer (art:, themes, font)
      config.py             config reading, keyboard, log_message, VERSION
    config/                 all .txt settings (see "Settings" below)
    docs/                   README.md, AGENTS.md, CHANGELOG.md, IDEAS.md
    deploy/                 vk-interactive-bot.service
    requirements.txt        python dependencies

## Setup

### 1) Create and configure a VK community

Create VK Group

Enable Messages for Group (Настройки, сообщения, сообщения сообщества) https://vk.ru/clubID/settings/messages

Enable Bot Capabilities (for inline_keyboard) (Настройки, сообщения, настройки для бота, возможности ботов) https://vk.ru/clubID/settings/bots

Enable Long Poll API (Настройки, дополнительно, работа с API, Long Poll API) https://vk.ru/clubID?act=longpoll_api

Setup access_token (Настройки, дополнительно, работа с API), доступы: сообщения сообщества, управление сообществом) https://vk.ru/clubID?act=tokens

### Access rights / Права доступа (важно!)

Для работы нужны права доступа сообщества (Настройки сообщества → Работа с API → Ключи доступа):

- `messages` — сообщения сообщества (основа, без него бот не работает);
- `photos` — загрузка **фотографий** в сообщения → нужны для **ART** (`art:`, ART-режим, картинки);
- `docs` — загрузка **документов** в сообщения → нужны для **`send <файл>`** и для документа-фолбэка картинок.

Без `photos` ART-картинка не отправится (фото-загрузка вернёт пусто); без `docs` не уйдёт `send` и не сработает фолбэк «документом».

## Setup (continued)

### 2) Clone and create a virtual environment

    cd /root
    git clone <repo-url> vk-api
    cd vk-api
    python3.12 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

### 3) Provide the secret config

    cp config/bot_config.txt.example config/bot_config.txt
    nano config/bot_config.txt      # TOKEN=... , ALLOWED_ADMINS=<id>[,<id>...]

## Settings (`config/`)

    config/bot_config.txt          SECRET: TOKEN=..., ALLOWED_ADMINS=ID,ID   (gitignored)
    config/bot_config.txt.example  template for the secret config
    config/entrypoint.txt          where/how the bash session starts: workdir=<dir>, init=<cmd>
    config/delay_config.txt        base delay between messages, sec (min 1.0, default 5.0)
    config/interactive_list.txt    comma-separated commands treated as interactive (live PTY)
    config/keyboard_enable.txt     true/false — show the VK keyboard at all (default true)
    config/keyboard_list.txt       custom buttons, one per line: "Name (command)"
    config/keyboard_custom.txt     true/false — show custom buttons (system row stays)
    config/idle_timeout.txt        seconds of inactivity before auto-closing the session (0 = off)
    config/version.txt             bot version (shown in debug headers)

## Run

Manual:

    source venv/bin/activate
    python3 run.py

With systemd (example unit in the repo: `deploy/vk-interactive-bot.service`):

    sudo cp deploy/vk-interactive-bot.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now vk-interactive-bot

Logs: stdout + `logs/info.log` (INFO/WARNING) and `logs/error.log` (ERROR).

## Available commands (in VK chat)

- normal command — runs in a live PTY bash session
- `art: <command>` — run in the active session and send result as an image (colors!)
  - `art:bg dark|light|auto` — image background; `art:font N` — font size
  - `art:last` — resend the last ART image
  - `art:doc <command>` — send result as a document (PNG) instead of a photo
- `bg: <command>` — run in background; output arrives in chat as it is produced
- `send <path|url>` — send a file from the server or by URL to the chat (as a document)
- `reset` / `сброс` / `exit` — reset the session
- `💀 Hard Reset` — kill a stuck session (new one is created on the next command)
- `ctrl` / `ctrl+c` — send SIGINT to interrupt output
- `status` / `статус` — bot/session/server diagnostics
- `🖼 ART` — all commands as images; `📄 Текст` — back to text
- `help` / `помощь` / `?` — this list
