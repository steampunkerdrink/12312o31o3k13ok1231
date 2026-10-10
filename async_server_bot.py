import asyncio
import logging
import os
import signal
import config_manager
import shell_worker
import terminal_render
from io import BytesIO
from PIL import Image as _PILImage
from vkbottle.bot import Bot, Message

# Универсальная загрузка фото/документов: в vkbottle 4.7+ отдельные классы, раньше — MessageUploader
try:
    from vkbottle import PhotoMessageUploader as _PhotoUploader, DocMessagesUploader as _DocUploader
except ImportError:
    from vkbottle import MessageUploader as _PhotoUploader
    _DocUploader = None

_mirrored = False


def _mirror_logging_to_file() -> None:
    """Дублирует логи/ошибки vkbottle (модуль logging) в bot_server.log.

    Раньше трейсбеки падали только в journalctl/systemd; здесь цепляем обработчик
    на корневой логгер, чтобы всё писало ещё и в наш файл.
    """
    global _mirrored
    if _mirrored:
        return
    _mirrored = True
    try:
        root = logging.getLogger()
        handler = logging.FileHandler("bot_server.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception as e:
        print(f"[mirror] Не удалось настроить файловый лог: {e}", flush=True)


_mirror_logging_to_file()

VERSION = config_manager.VERSION
TOKEN, ALLOWED_ADMINS = config_manager.load_secret_config()

bot = Bot(token=TOKEN)
user_sessions = {}

# Фото-загрузчик: дефолтное имя picture.jpg — именно с ним ls -la успешно загружалось фото.
photo_uploader = _PhotoUploader(bot.api)
# Фолбэк-загрузчик картинки как документа: имя .jpg, чтобы VK показывал его как изображение.
try:
    doc_uploader = _DocUploader(bot.api, attachment_name="output.jpg") if _DocUploader is not None else None
except TypeError:
    doc_uploader = _DocUploader(bot.api) if _DocUploader is not None else None

# Единый список команд внутреннего хард-ресета (используется в двух местах ниже)
HARD_RESET_COMMANDS = ["hard-reset", "убей сессию", "kill-session", "💀 hard reset"]

async def get_or_create_session(user_id: int, api):
    if user_id in user_sessions:
        session = user_sessions[user_id]
        if session["process"].returncode is None: return session

    config_manager.log_message("INFO", f"Запуск новой интерактивной bash-сессии для ID {user_id}")
    
    master_fd, slave_fd = os.openpty()
    process = await asyncio.create_subprocess_exec(
        "/bin/bash", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, preexec_fn=os.setsid
    )
    os.close(slave_fd)
    
    loop = asyncio.get_event_loop()
    process_reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(process_reader)
    await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb"))
    
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(), os.fdopen(os.dup(master_fd), "wb")
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, process_reader, loop)

    listen_task = asyncio.create_task(shell_worker.start_output_listener(user_id, process_reader, api, process))
    
    user_sessions[user_id] = {
        "process": process, 
        "writer": writer, 
        "task": listen_task,
        "master_fd": master_fd
    }
    shell_worker.user_interactive_mode[user_id] = False
    
    await asyncio.sleep(0.3)
    init_cmd = "stty -echo\nstty -icanon\necho ''\necho \"___END_OF_COMMAND___:\"$(pwd)\n"
    writer.write(init_cmd.encode("utf-8"))
    await writer.drain()
    return user_sessions[user_id]

async def run_as_art(user_id: int, command: str, message: Message):
    """Выполняет команду в текущей папке сессии и присылает результат картинкой (PNG)."""
    if not command:
        await message.answer("Пустая команда. Пример: art: qrencode -t ansiutf8 https://...")
        return
    config_manager.log_message("INFO", f"Админ [{user_id}] art-выполнение: {command}")
    full = f"cd {shell_worker.last_valid_path} && {command}"
    try:
        proc = await asyncio.create_subprocess_shell(
            full, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await message.answer("Команда выполнялась дольше 120 c — прервано.")
        return
    except Exception as e:
        config_manager.log_message("ERROR", f"art-команда не выполнилась: {e}")
        await message.answer(f"Ошибка выполнения: {e}")
        return
    raw = raw or b""
    if proc.returncode != 0:
        out = raw.decode("utf-8", errors="replace").strip()
        await message.answer(f"Команда вернула код {proc.returncode}:\n{out[-2000:]}")
        return
    # 1) Рендерим PNG и перекодируем в JPEG (фото-сервер VK надёжнее принимает JPEG)
    try:
        png = terminal_render.render(raw)
        img = _PILImage.open(BytesIO(png)).convert("RGB")
        jpeg_buf = BytesIO()
        img.save(jpeg_buf, format="JPEG", quality=90)
        upload_bytes = jpeg_buf.getvalue()
    except Exception as e:
        config_manager.log_message("ERROR", f"Ошибка рендера art-вывода: {e}")
        await message.answer(
            f"Не удалось отрисовать картинку: {e}\nСырой вывод:\n"
            + raw.decode("utf-8", errors="replace")[-2000:]
        )
        return

    # 2) Отправляем как фото (несколько попыток + таймаут)
    photo_err = None
    for _attempt in range(1, 4):
        try:
            if hasattr(photo_uploader, "upload"):
                attachment = await asyncio.wait_for(
                    photo_uploader.upload(file_source=upload_bytes, peer_id=user_id), timeout=30
                )
            else:
                attachment = await asyncio.wait_for(
                    photo_uploader.upload_photos(file_source=upload_bytes, peer_id=user_id), timeout=30
                )
            await message.answer(attachment=attachment)
            config_manager.log_message("INFO", f"Админ [{user_id}] art-картинка отправлена фото")
            return
        except Exception as e_photo:
            photo_err = e_photo
            config_manager.log_message("WARNING", f"Фото-загрузка не удалась ({e_photo}); попытка {_attempt}/3")
            await asyncio.sleep(1)

    # 3) Фолбэк: отправляем как документ (с таймаутом)
    if doc_uploader is not None:
        try:
            doc_attachment = await asyncio.wait_for(
                doc_uploader.upload(file_source=upload_bytes, peer_id=user_id), timeout=30
            )
            config_manager.log_message("INFO", f"Админ [{user_id}] art-картинка отправлена документом")
            await message.answer(
                message=f"⚠️ Фото-загрузка не прошла ({photo_err}); отправил изображением.",
                attachment=doc_attachment,
            )
            return
        except Exception as e_doc:
            config_manager.log_message("ERROR", f"Фото: {photo_err}; документ: {e_doc}")
            await message.answer(
                f"Не удалось отправить картинку (фото: {photo_err}; документ: {e_doc}).\n"
                f"Сырой вывод:\n{raw.decode('utf-8', errors='replace')[-1500:]}"
            )
            return

    await message.answer(
        f"Не удалось отправить картинку (фото: {photo_err}).\n"
        f"Сырой вывод:\n{raw.decode('utf-8', errors='replace')[-1500:]}"
    )


async def handle_art_command(user_id: int, text: str, message: Message):
    """Обработчик префикса «art:» — всегда рендерит результат картинкой."""
    cmd = text.split(":", 1)[1].strip()
    await run_as_art(user_id, cmd, message)


@bot.on.message()
async def handle_message(message: Message):
    user_id = message.from_id
    text_input = message.text.strip()
    if not text_input: return

    if user_id not in ALLOWED_ADMINS:
        config_manager.log_message("WARNING", f"Попытка доступа от неавторизованного ID {user_id}.")
        return

    # Переключение режима вывода: картинкой (ART) или текстом
    lower_text = text_input.lower()
    if lower_text in ["🖼 art", "art-mode"]:
        config_manager.art_mode = True
        config_manager.log_message("INFO", f"Админ [{user_id}] включил ART-режим")
        await message.answer("🖼 ART-режим включён: команды приходят картинкой. Чтобы вернуть текст — нажми «📄 Текст».")
        return
    if lower_text in ["📄 текст", "text-mode", "textmode"]:
        config_manager.art_mode = False
        config_manager.log_message("INFO", f"Админ [{user_id}] выключил ART-режим (текст)")
        await message.answer("📄 Текст-режим: команды приходят текстом (кроме «art: …»).")
        return

    # ART-РЕЖИМ: выполнить команду и прислать результат картинкой (PNG)
    if text_input.lower().startswith("art:"):
        try:
            await handle_art_command(user_id, text_input, message)
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка art-обработки: {e}")
            await message.answer(f"Ошибка при выполнении: {e}")
        return

    current_kb = config_manager.generate_dynamic_keyboard()

    # СТАНДАРТНЫЙ СБРОС (v5.3.0)
    # Исключаем хард-ресет: иначе подстрока "reset" в "💀 hard reset" (текст кнопки
    # Hard Reset) срабатывала бы обычным сбросом раньше, чем внутренний хард-ресет.
    lower_text = text_input.lower()
    is_hard_reset = lower_text in HARD_RESET_COMMANDS
    if not is_hard_reset and ("сброс" in lower_text or "reset" in lower_text or lower_text == "exit"):
        if user_id in user_sessions:
            session = user_sessions[user_id]
            session["task"].cancel()
            await asyncio.sleep(0.1)
            try: os.close(session["master_fd"])
            except: pass
            try: os.killpg(os.getpgid(session["process"].pid), signal.SIGKILL)
            except: pass
            del user_sessions[user_id]
        shell_worker.user_interactive_mode[user_id] = False
        shell_worker.current_flood_multiplier = 1.0
        shell_worker.last_valid_path = "/root"
        current_ver = config_manager.get_version()
        await bot.api.messages.send(peer_id=user_id, message=f"🔄 Сессия очищена, задержки сброшены. [v{current_ver}]", keyboard=current_kb, random_id=0)
        return

    # ВНУТРЕННИЙ ХАРД-РЕСЕТ СЕССИИ (v5.3.0)
    if text_input.lower() in HARD_RESET_COMMANDS:
        if user_id in user_sessions:
            session = user_sessions[user_id]
            session["task"].cancel()
            await asyncio.sleep(0.05)
            try:
                os.killpg(os.getpgid(session["process"].pid), signal.SIGKILL)
                os.close(session["master_fd"])
            except: pass
            del user_sessions[user_id]
            
        shell_worker.user_interactive_mode[user_id] = False
        shell_worker.current_flood_multiplier = 1.0
        shell_worker.last_valid_path = "/root"
        
        await get_or_create_session(user_id, bot.api)
        await message.answer("💀 [INTERNAL HARD-RESET]: Зависшая сессия bash полностью уничтожена. Создан новый чистый терминал!")
        return

    if text_input.lower() in ["turbo", "турбо", "⚡ турбо"]:
        shell_worker.current_flood_multiplier = 1.0
        config_manager.log_message("INFO", f"Админ [{user_id}] вручную сбросил штрафной множитель задержки в 1.0")
        await message.answer("⚡ Коэффициент флуд-контроля ВК принудительно сброшен на 1.0!")
        return

    # ЖЕЛЕЗНЫЙ CTRL+C (v5.3.0): Шлет только байт прерывания. Идеально для вывода статистики!
    if "ctrl" in text_input.lower() or "сигнал" in text_input.lower() or "ctrl+c" in text_input.lower():
        if user_id in user_sessions and user_sessions[user_id]["process"].returncode is None:
            session = user_sessions[user_id]
            config_manager.log_message("INFO", f"Админ [{user_id}] шлет физический сигнал Ctrl+C")
            
            try:
                os.write(session["master_fd"], b"\x03")
            except Exception as e_signal:
                config_manager.log_message("ERROR", f"Сбой отправки байта прерывания: {e_signal}")
                
            shell_worker.user_interactive_mode[f"{user_id}_last_cmd"] = "SIGINT (Ctrl+C)"
        return

    if text_input in config_manager.dynamic_commands_map:
        command = config_manager.dynamic_commands_map[text_input]
    else:
        command = text_input

    shell_worker.user_interactive_mode[f"{user_id}_last_cmd"] = command
    config_manager.log_message("INFO", f"Админ [{user_id}] отправил: {command}")

    # Если включён ART-режим — обычные команды шлём картинкой.
    # Исключение: интерактивные команды (ssh, apt, nano, mysql, ufw…) всё равно
    # идут в живую PTY-сессию, чтобы работать интерактивно (у art-подпроцесса лимит 120c).
    if config_manager.art_mode and not config_manager.is_command_interactive(command):
        try:
            await run_as_art(user_id, command, message)
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка art-обработки: {e}")
            await message.answer(f"Ошибка при выполнении: {e}")
        return

    session_data = await get_or_create_session(user_id, bot.api)
    writer = session_data["writer"]

    try:
        if shell_worker.user_interactive_mode.get(user_id, False):
            writer.write((command + "\n").encode("utf-8"))
            await writer.drain()
        else:
            is_interactive = config_manager.is_command_interactive(command)
            
            if is_interactive:
                shell_worker.user_interactive_mode[user_id] = True
                full_command = f"{command}\n"
                config_manager.log_message("INFO", f"Включен интерактивный режим для команды: {command}")
            else:
                full_command = command + "\necho ''\necho \"___END_OF_COMMAND___:\"$(pwd)\n"
                
            writer.write(full_command.encode("utf-8"))
            await writer.drain()
            
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки команды: {str(e)}")

if __name__ == "__main__":
    current_ver = config_manager.get_version()
    config_manager.log_message("INFO", f"Запуск модульного безопасного СТРИМ-CLI-бота (v{VERSION})...")
    bot.run()
