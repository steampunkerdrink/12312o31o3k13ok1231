import os
from vkbottle import Keyboard, KeyboardButtonColor, Text

CONFIG_FILE = "delay_config.txt"
KEYBOARD_FILE = "keyboard_list.txt"
SECRET_CONFIG_FILE = "bot_config.txt"
KEYBOARD_TOGGLE_FILE = "keyboard_enable.txt"
VERSION_FILE = "version.txt"
INTERACTIVE_FILE = "interactive_list.txt"

dynamic_commands_map = {}

def get_version() -> str:
    """Считывает версию из файла version.txt на лету. По умолчанию 4.0.0"""
    if not os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "w", encoding="utf-8") as f: f.write("4.0.0")
        except: pass
        return "4.0.0"
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f: return f.read().strip()
    except: return "4.0.0"

def load_secret_config():
    """Считывает ТОКЕН и список ADMINS из bot_config.txt"""
    default_token = "vk1.a.your_token_here"
    if not os.path.exists(SECRET_CONFIG_FILE):
        try:
            with open(SECRET_CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(f"TOKEN={default_token}\nALLOWED_ADMINS=171474445\n")
        except: pass
        return default_token, []

    token = default_token
    admins = []
    try:
        with open(SECRET_CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip().upper()
                    value = value.strip()
                    if key == "TOKEN" and value: token = value
                    elif key == "ALLOWED_ADMINS" and value: admins = [int(x.strip()) for x in value.split(",") if x.strip().isdigit()]
    except: pass
    return token, admins

def get_dynamic_delay() -> float:
    """Считывает задержку отправки логов из файла."""
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f: f.write("5.0")
        except: pass
        return 5.0
    try:
        with open(CONFIG_FILE, "r") as f: return max(1.0, float(f.read().strip()))
    except: return 5.0

def is_keyboard_enabled() -> bool:
    """Проверяет файл keyboard_enable.txt на лету."""
    if not os.path.exists(KEYBOARD_TOGGLE_FILE):
        try:
            with open(KEYBOARD_TOGGLE_FILE, "w", encoding="utf-8") as f: f.write("true")
        except: pass
        return True
    try:
        with open(KEYBOARD_TOGGLE_FILE, "r", encoding="utf-8") as f: return f.read().strip().lower() == "true"
    except: return True

def get_interactive_commands() -> list:
    """Считывает список интерактивных утилит из файла на лету."""
    default_cmds = ["ssh", "apt", "nano", "mysql", "ufw"]
    if not os.path.exists(INTERACTIVE_FILE):
        try:
            with open(INTERACTIVE_FILE, "w", encoding="utf-8") as f: f.write(",".join(default_cmds))
        except: pass
        return default_cmds
    try:
        with open(INTERACTIVE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content: return default_cmds
            return [x.strip().lower() for x in content.split(",") if x.strip()]
    except: return default_cmds

def is_command_interactive(cmd_text: str) -> bool:
    """Проверяет, должна ли команда запускаться в интерактивном режиме PTY."""
    cmd_lower = cmd_text.strip().lower()
    if not cmd_lower: return False
    
    interactive_list = get_interactive_commands()
    if any(cmd_lower.startswith(x + " ") or cmd_lower == x for x in interactive_list):
        return True
        
    if "openflux" in cmd_lower and not cmd_lower.startswith("cd ") and not cmd_lower.startswith("ls "):
        return True
        
    return False

def generate_dynamic_keyboard() -> str:
    """Читает кастомные опции из файла кнопок и жестко добавляет Сброс."""
    global dynamic_commands_map
    
    if not is_keyboard_enabled():
        return '{"buttons":[],"one_time":true}'
        
    kb = Keyboard(one_time=False, inline=False)
    dynamic_commands_map["📴 Off"] = "echo 'false' > keyboard_enable.txt"
    
    if os.path.exists(KEYBOARD_FILE):
        try:
            with open(KEYBOARD_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
        except: lines = []
    else:
        lines = ["mytest (ssh test@test.com)", "mytest2 (pwd)"]

    buttons_count = 0
    for line in lines:
        clean_line = line.strip()
        if "keyboard_enable.txt" in clean_line: continue
        if "(" in clean_line and clean_line.endswith(")"):
            try:
                # ИСПРАВЛЕНО И ПРОВЕРЕНО (v4.0.0): Железобетонные строковые срезы без списков rsplit!
                btn_name, raw_cmd = clean_line.split("(", 1)
                btn_name = btn_name.strip()
                btn_cmd = raw_cmd[:-1].strip() # Просто срезаем последнюю закрывающую скобку ')'
                
                if btn_name and btn_cmd:
                    dynamic_commands_map[btn_name] = btn_cmd
                    
                    if buttons_count > 0 and buttons_count % 4 == 0: 
                        kb.row()
                    color = KeyboardButtonColor.POSITIVE if "старт" in btn_name.lower() else KeyboardButtonColor.PRIMARY
                    kb.add(Text(btn_name), color=color)
                    buttons_count += 1
            except: pass

    if buttons_count > 0: kb.row()
    kb.add(Text("⌨️ Ctrl+C"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("📴 Off"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🔄 Reset"), color=KeyboardButtonColor.NEGATIVE)
    
    return kb.get_json()
