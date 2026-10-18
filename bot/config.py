import os
from datetime import datetime
from vkbottle import Keyboard, KeyboardButtonColor, Text

# Корень проекта — папка выше пакета bot/ (там лежат requirements.txt, config/, docs/, deploy/).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
try:
    os.makedirs(CONFIG_DIR, exist_ok=True)
except Exception:
    pass


def _cfg(name: str) -> str:
    """Абсолютный путь к файлу конфигурации в каталоге config/."""
    return os.path.join(CONFIG_DIR, name)


CONFIG_FILE = _cfg("delay_config.txt")
KEYBOARD_FILE = _cfg("keyboard_list.txt")
SECRET_CONFIG_FILE = _cfg("bot_config.txt")
KEYBOARD_TOGGLE_FILE = _cfg("keyboard_enable.txt")
VERSION_FILE = _cfg("version.txt")
INTERACTIVE_FILE = _cfg("interactive_list.txt")
KEYBOARD_CUSTOM_FILE = _cfg("keyboard_custom.txt")
IDLE_FILE = _cfg("idle_timeout.txt")
ENTRYPOINT_FILE = _cfg("entrypoint.txt")

# Логи пишем в logs/ двумя потоками: info.log (INFO/WARNING) и error.log (ERROR).
LOG_DIR = os.path.join(BASE_DIR, "logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass
INFO_LOG_FILE = os.path.join(LOG_DIR, "info.log")
ERROR_LOG_FILE = os.path.join(LOG_DIR, "error.log")

# Каталог для временных файлов (напр. картинка, которую VK не принял).
TMP_DIR = os.path.join(BASE_DIR, "tmp")
try:
    os.makedirs(TMP_DIR, exist_ok=True)
except Exception:
    pass

# Единый источник версии для всего проекта (замечание №2 в AGENTS.md)
VERSION = "5.16.0"

dynamic_commands_map = {}

# Глобальный переключатель «art-режима»: если True — обычные команды бот присылает
# картинкой (PNG). Управляется кнопками на клавиатуре.
art_mode = False


def log_message(level: str, message: str):
    """Единый логгер проекта: метка времени, stdout + файл в logs/ (info.log или error.log)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"{timestamp} [{level}] {message}"
    print(log_line, flush=True)
    target = ERROR_LOG_FILE if level.upper() == "ERROR" else INFO_LOG_FILE
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as e_log:
        print(f"[log_message] Не удалось записать лог в файл: {e_log}", flush=True)


def get_version() -> str:
    """Считывает версию из файла version.txt на лету. По умолчанию — VERSION."""
    if not os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "w", encoding="utf-8") as f:
                f.write(VERSION)
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {VERSION_FILE}: {e}")
        return VERSION
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {VERSION_FILE}: {e}")
        return VERSION


def get_entrypoint() -> dict:
    """Читает entrypoint.txt: где и как стартует bash-сессия.

    Ключи: `workdir=<путь>` — стартовая папка сессии; `init=<команда>` — доп. инициализация.
    """
    result = {"workdir": "/root", "init": ""}
    if not os.path.exists(ENTRYPOINT_FILE):
        try:
            with open(ENTRYPOINT_FILE, "w", encoding="utf-8") as f:
                f.write(
                    "# entrypoint.txt — где и как стартует bash-сессия бота.\n"
                    "# workdir — стартовая рабочая папка сессии (в неё делается cd при создании)\n"
                    "workdir=/root\n"
                    "# init — дополнительные команды инициализации сессии (одна строка, команды через ';')\n"
                    "init=\n"
                )
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {ENTRYPOINT_FILE}: {e}")
        return result
    try:
        with open(ENTRYPOINT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key == "workdir" and value:
                    result["workdir"] = value
                elif key == "init":
                    result["init"] = value
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {ENTRYPOINT_FILE}: {e}")
    return result

def load_secret_config():
    """Считывает ТОКЕН и список ADMINS из bot_config.txt"""
    default_token = "vk1.a.your_token_here"
    if not os.path.exists(SECRET_CONFIG_FILE):
        try:
            with open(SECRET_CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(f"TOKEN={default_token}\nALLOWED_ADMINS=171474445\n")
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {SECRET_CONFIG_FILE}: {e}")
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
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {SECRET_CONFIG_FILE}: {e}")
    return token, admins

def get_dynamic_delay() -> float:
    """Считывает задержку отправки логов из файла."""
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f: f.write("5.0")
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {CONFIG_FILE}: {e}")
        return 5.0
    try:
        with open(CONFIG_FILE, "r") as f: return max(1.0, float(f.read().strip()))
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {CONFIG_FILE}: {e}")
        return 5.0

def is_keyboard_enabled() -> bool:
    """Проверяет файл keyboard_enable.txt на лету."""
    if not os.path.exists(KEYBOARD_TOGGLE_FILE):
        try:
            with open(KEYBOARD_TOGGLE_FILE, "w", encoding="utf-8") as f: f.write("true")
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {KEYBOARD_TOGGLE_FILE}: {e}")
        return True
    try:
        with open(KEYBOARD_TOGGLE_FILE, "r", encoding="utf-8") as f: return f.read().strip().lower() == "true"
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {KEYBOARD_TOGGLE_FILE}: {e}")
        return True

def is_custom_keyboard_enabled() -> bool:
    """Читает keyboard_custom.txt: показывать ли пользовательские кнопки (по умолчанию да)."""
    if not os.path.exists(KEYBOARD_CUSTOM_FILE):
        try:
            with open(KEYBOARD_CUSTOM_FILE, "w", encoding="utf-8") as f: f.write("true")
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {KEYBOARD_CUSTOM_FILE}: {e}")
        return True
    try:
        with open(KEYBOARD_CUSTOM_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().lower() == "true"
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {KEYBOARD_CUSTOM_FILE}: {e}")
        return True


def get_idle_timeout() -> int:
    """Секунды простоя до авто-закрытия сессии. 0 = выключено (по умолчанию 0)."""
    if not os.path.exists(IDLE_FILE):
        try:
            with open(IDLE_FILE, "w", encoding="utf-8") as f: f.write("0")
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {IDLE_FILE}: {e}")
        return 0
    try:
        with open(IDLE_FILE, "r", encoding="utf-8") as f:
            return max(0, int(float(f.read().strip())))
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {IDLE_FILE}: {e}")
        return 0


def get_interactive_commands() -> list:
    """Считывает список интерактивных утилит из файла на лету."""
    default_cmds = ["ssh", "apt", "nano", "mysql", "ufw"]
    if not os.path.exists(INTERACTIVE_FILE):
        try:
            with open(INTERACTIVE_FILE, "w", encoding="utf-8") as f: f.write(",".join(default_cmds))
        except Exception as e:
            log_message("ERROR", f"Не удалось создать {INTERACTIVE_FILE}: {e}")
        return default_cmds
    try:
        with open(INTERACTIVE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content: return default_cmds
            return [x.strip().lower() for x in content.split(",") if x.strip()]
    except Exception as e:
        log_message("ERROR", f"Не удалось прочитать {INTERACTIVE_FILE}: {e}")
        return default_cmds

def is_command_interactive(cmd_text: str) -> bool:
    """Проверяет, должна ли команда запускаться в интерактивном режиме PTY."""
    cmd_lower = cmd_text.strip().lower()
    if not cmd_lower:
        return False

    # Имя команды — первый токен, пропуская обёртки (sudo/env/nohup/…), плюс basename,
    # чтобы `/usr/bin/apt` и `./openflux2` определялись как apt/openflux2.
    tokens = cmd_lower.split()
    wrappers = {"sudo", "doas", "env", "nohup", "command", "nice", "time", "setsid"}
    i = 0
    while i < len(tokens) and tokens[i] in wrappers:
        i += 1
    cmd_base = os.path.basename(tokens[i]) if i < len(tokens) else ""

    interactive_list = get_interactive_commands()
    # Сравниваем именно ИМЯ команды: `sudo apt install X` → apt (интерактив),
    # а `cp -R OpenFlux2 X` → cp (обычная).
    if cmd_base in interactive_list:
        return True
    # На случай записей с аргументами в interactive_list (старое поведение)
    if any(cmd_lower.startswith(x + " ") or cmd_lower == x for x in interactive_list):
        return True

    # Особый случай проекта: интерактивен ЗАПУСК openflux (имя команды), а не упоминание
    # его в аргументах — иначе `cp -R OpenFlux2 OpenFluxRM` ложно уходил в интерактив.
    if "openflux" in cmd_base:
        return True

    return False

def generate_dynamic_keyboard() -> str:
    """Читает кастомные опции из файла кнопок и жестко добавляет Сброс."""
    global dynamic_commands_map
    
    if not is_keyboard_enabled():
        return '{"buttons":[],"one_time":true}'
        
    kb = Keyboard(one_time=False, inline=False)
    dynamic_commands_map["📴 Off"] = f"echo 'false' > {KEYBOARD_TOGGLE_FILE}"
    # Маппим аварийную кнопку хард-ресета
    dynamic_commands_map["💀 Hard Reset"] = "hard-reset"
    
    if os.path.exists(KEYBOARD_FILE):
        try:
            with open(KEYBOARD_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines() if line.strip()]
        except Exception as e:
            log_message("ERROR", f"Не удалось прочитать {KEYBOARD_FILE}: {e}")
            lines = []
    else:
        lines = ["mytest (ssh test@test.com)", "mytest2 (pwd)"]

    # Тоггл пользовательских кнопок: keyboard_custom.txt = false скрывает их (системный ряд остаётся)
    if not is_custom_keyboard_enabled():
        lines = []

    buttons_count = 0
    for line in lines:
        clean_line = line.strip()
        if "keyboard_enable.txt" in clean_line: continue
        if "(" in clean_line and clean_line.endswith(")"):
            try:
                btn_name, raw_cmd = clean_line.split("(", 1)
                btn_name = btn_name.strip()
                btn_cmd = raw_cmd[:-1].strip()
                
                if btn_name and btn_cmd:
                    dynamic_commands_map[btn_name] = btn_cmd
                    if buttons_count > 0 and buttons_count % 4 == 0: kb.row()
                    color = KeyboardButtonColor.POSITIVE if "старт" in btn_name.lower() else KeyboardButtonColor.PRIMARY
                    kb.add(Text(btn_name), color=color)
                    buttons_count += 1
            except Exception as e:
                log_message("ERROR", f"Не удалось разобрать кнопку '{clean_line}': {e}")

    if buttons_count > 0: kb.row()
    # ИСПРАВЛЕНО (v5.2.0): Системный ряд теперь содержит 4 компактные кнопки
    kb.add(Text("⌨️ Ctrl+C"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("📴 Off"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("💀 Hard Reset"), color=KeyboardButtonColor.SECONDARY) # Белая кнопка спасения
    kb.add(Text("🔄 Reset"), color=KeyboardButtonColor.NEGATIVE)

    # Переключатели режима вывода: картинкой (ART) или текстом (Текст)
    kb.row()
    kb.add(Text("🖼 ART"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📄 Текст"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📊 Статус"), color=KeyboardButtonColor.SECONDARY)

    return kb.get_json()
