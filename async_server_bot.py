import asyncio
import os
import signal
import config_manager
import shell_worker
from vkbottle.bot import Bot, Message

VERSION = "5.1.0"
TOKEN, ALLOWED_ADMINS = config_manager.load_secret_config()

bot = Bot(token=TOKEN)
user_sessions = {}
last_valid_path = "/root"

async def get_or_create_session(user_id: int, api):
    if user_id in user_sessions:
        session = user_sessions[user_id]
        if session["process"].returncode is None: return session

    shell_worker.log_message("INFO", f"Запуск новой интерактивной bash-сессии для ID {user_id}")
    
    # Создаем псевдотерминал PTY на уровне ОС
    master_fd, slave_fd = os.openpty()
    
    # ИСПРАВЛЕНО (v5.1.0): Прямой и безопасный запуск подпроцесса через дескрипторы PTY ОС Linux
    # Это на корню убирает ошибку "Socket operation on non-socket"
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid
    )
    # Сразу закрываем слейв-дескриптор в родительском потоке, он теперь принадлежит bash
    os.close(slave_fd)
    
    # Обертываем master_fd в стандартные асинхронные потоки чтения/записи Python
    loop = asyncio.get_event_loop()
    
    # Используем файловые потоки вместо сокетных пайпов
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, os.fdopen(master_fd, "rb"))
    
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(), os.fdopen(master_fd, "wb")
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, loop)

    # Запускаем нашего фонового слушателя
    listen_task = asyncio.create_task(shell_worker.start_output_listener(user_id, reader, api, process))
    
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
        shell_worker.log_message("WARNING", f"Попытка доступа от неавторизованного ID {user_id}.")
        return

    current_kb = config_manager.generate_dynamic_keyboard()

    # СБРОС СЕССИИ (v5.1.0)
    if "сброс" in text_input.lower() or "reset" in text_input.lower() or text_input.lower() == "exit":
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

    if text_input.lower() in ["turbo", "турбо", "⚡ турбо"]:
        shell_worker.current_flood_multiplier = 1.0
        shell_worker.log_message("INFO", f"Админ [{user_id}] вручную сбросил штрафной множитель задержки в 1.0")
        await message.answer("⚡ Коэффициент флуд-контроля ВК принудительно сброшен на 1.0!")
        return

    # СИГНАЛ ПРЕРЫВАНИЯ (v5.1.0)
    if "ctrl" in text_input.lower() or "сигнал" in text_input.lower() or "ctrl+c" in text_input.lower():
        if user_id in user_sessions and user_sessions[user_id]["process"].returncode is None:
            session = user_sessions[user_id]
            writer = session["writer"]
            shell_worker.log_message("INFO", f"Админ [{user_id}] послал Ctrl+C в PTY трубу")
            
            try:
                os.write(session["master_fd"], b"\x03")
            except Exception as e_signal:
                shell_worker.log_message("ERROR", f"Сбой отправки байта прерывания: {e_signal}")
                
            shell_worker.user_interactive_mode[user_id] = False
            shell_worker.user_interactive_mode[f"{user_id}_last_cmd"] = "SIGINT (Ctrl+C)"
            
            exit_cmd = f"\r\necho ''\necho \"___END_OF_COMMAND___:{shell_worker.last_valid_path}\"\n"
            writer.write(exit_cmd.encode("utf-8"))
            await writer.drain()
        return

    if text_input in config_manager.dynamic_commands_map:
        command = config_manager.dynamic_commands_map[text_input]
    else:
        command = text_input

    shell_worker.user_interactive_mode[f"{user_id}_last_cmd"] = command
    shell_worker.log_message("INFO", f"Админ [{user_id}] отправил: {command}")
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
                shell_worker.log_message("INFO", f"Включен интерактивный режим для команды: {command}")
            else:
                full_command = command + "\necho ''\necho \"___END_OF_COMMAND___:\"$(pwd)\n"
                
            writer.write(full_command.encode("utf-8"))
            await writer.drain()
            
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки команды: {str(e)}")

if __name__ == "__main__":
    current_ver = config_manager.get_version()
    shell_worker.log_message("INFO", f"Запуск модульного безопасного СТРИМ-CLI-бота (v{VERSION})...")
    bot.run()
