import asyncio
import os
import signal
import config_manager
import shell_worker
from vkbottle.bot import Bot, Message

VERSION = config_manager.VERSION
TOKEN, ALLOWED_ADMINS = config_manager.load_secret_config()

bot = Bot(token=TOKEN)
user_sessions = {}

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

@bot.on.message()
async def handle_message(message: Message):
    user_id = message.from_id
    text_input = message.text.strip()
    if not text_input: return

    if user_id not in ALLOWED_ADMINS:
        config_manager.log_message("WARNING", f"Попытка доступа от неавторизованного ID {user_id}.")
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
