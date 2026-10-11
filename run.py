# -*- coding: utf-8 -*-
"""Точка входа проекта: запускает VK CLI-бота.

Использование:  python3 run.py   (или через systemd, см. deploy/vk-interactive-bot.service).
"""
from bot.app import main

if __name__ == "__main__":
    main()
