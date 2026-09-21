import asyncio
import re
from datetime import datetime
import config_manager

VERSION = "5.2.1"
MARKER = "___END_OF_COMMAND___"
user_interactive_mode = {}
current_flood_multiplier = 1.0

# ИСПРАВЛЕНО (v5.2.1): Кэш путей теперь монолитно живёт здесь, решая проблему Circular Import!
last_valid_path = "/root"

# Синтаксически корректный Regex для удаления ANSI-кодов цвета
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9;?]*[a-zA-Z])')

def log_message(level: str, message: str):
    """Кастомный безопасный логер"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"{timestamp} [{level}] {message}"
    print(log_line, flush=True)
    try:
        with open("bot_server.log", "a", encoding="utf-8") as f: f.write(log_line + "\n")
    except: pass

def clean_terminal_garbage(text: str) -> str:
    """Очищает вывод от ANSI-кодов и скрытых системных хвостов PTY"""
    text = ANSI_ESCAPE.sub('', text)
    text = text.replace("[?2004h", "").replace("[?2004l", "")
    
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if ("root@" in line and stripped.endswith("#")) or "@v2858132" in line or stripped.endswith("#") or "stty " in line or MARKER in line:
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

async def send_safe_message(api, peer_id: int, text: str, is_interactive: bool = False):
    """Нарезает длинный текст на куски и шлет в ВК порциями с адаптивным таймаутом"""
    global current_flood_multiplier
    if not text.strip(): return
    
    max_len = 3500
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    current_kb = config_manager.generate_dynamic_keyboard()

    for chunk in chunks:
        if not chunk.strip(): continue
        while True:
            try:
                base_delay = config_manager.get_dynamic_delay()
                actual_delay = base_delay * current_flood_multiplier
                
                msg_body = chunk
                if is_interactive:
                    msg_body = f"⚠️ [PTY INTERACTIVE MODE]\n---------------------------\n{chunk}"
                    
                await api.messages.send(peer_id=peer_id, message=f"```\n{msg_body}\n```", keyboard=current_kb, random_id=0)
                if current_flood_multiplier > 1.0:
                    current_flood_multiplier = max(1.0, current_flood_multiplier - 0.5)
                await asyncio.sleep(actual_delay)
                break
            except Exception as e:
                err_msg = str(e).lower()
                if "flood" in err_msg or "disallowed" in err_msg or "api_error" in err_msg:
                    current_flood_multiplier = min(10.0, current_flood_multiplier * 2.5)
                    await asyncio.sleep(base_delay * current_flood_multiplier)
                    continue
                else:
                    await asyncio.sleep(2.0)
                    break

async def start_output_listener(user_id: int, process_reader, api, process):
    """Фоновый слушатель логов v5.2.1 (Исправлено сохранение путей и выгрузка статистики)"""
    global last_valid_path
    log_message("INFO", f"Запущено фоновое чтение PTY для пользователя {user_id} (v{VERSION})")
    
    buffer = []
    last_send_time = asyncio.get_event_loop().time()
    was_cancelled = False

    try:
        while process.returncode is None:
            try:
                chunk_bytes = await asyncio.wait_for(process_reader.read(4096), timeout=0.2)
                if not chunk_bytes: break
                    
                decoded_chunk = chunk_bytes.decode("utf-8", errors="replace")

                if MARKER in decoded_chunk:
                    user_interactive_mode[user_id] = False
                    current_kb = config_manager.generate_dynamic_keyboard()
                    
                    current_path = "/root"
                    try:
                        clean_marker_line = ANSI_ESCAPE.sub('', decoded_chunk).strip()
                        if MARKER in clean_marker_line:
                            idx = clean_marker_line.index(MARKER)
                            leftover = clean_marker_line[:idx].replace("\r", "").replace("\n", "").strip()
                            if leftover and leftover != "stty -echo":
                                buffer.append(leftover + "\n")
                            
                            raw_path = clean_marker_line[idx + len(MARKER):].replace("\r", "").replace("\n", "").strip()
                            current_path = raw_path.replace(":", "").strip()
                            
                            # ИСПРАВЛЕНО (v5.2.1): Обновляем локальный кэш без опасных импортов!
                            if current_path and "#" not in current_path and "/" in current_path:
                                last_valid_path = current_path
                    except Exception as e_parse:
                        log_message("ERROR", f"Ошибка парсинга пути: {e_parse}")
                    
                    if not current_path or "#" in current_path:
                        current_path = "/"

                    await asyncio.sleep(0.05)
                    while True:
                        try:
                            extra = await asyncio.wait_for(process_reader.read(1024), timeout=0.02)
                            if extra: buffer.append(extra.decode("utf-8", errors="replace"))
                            else: break
                        except asyncio.TimeoutError: break

                    raw_text = "".join(buffer)
                    buffer.clear()
                    
                    clean_text = clean_terminal_garbage(raw_text)
                    if clean_text:
                        clean_text = f"{clean_text}\n\n[EOF]"

                    fresh_ver = config_manager.get_version()
                    executed_command = user_interactive_mode.get(f"{user_id}_last_cmd", "command")
                    path_header = f"📁 [v{fresh_ver}] root@v2858132:{current_path}# {executed_command}"

                    if clean_text:
                        full_package = f"{path_header}\n```\n{clean_text}\n```"
                        await api.messages.send(peer_id=user_id, message=full_package, keyboard=current_kb, random_id=0)
                    else:
                        await api.messages.send(peer_id=user_id, message=path_header, keyboard=current_kb, random_id=0)
                    
                    last_send_time = asyncio.get_event_loop().time()
                    continue

                buffer.append(decoded_chunk)

            except asyncio.TimeoutError:
                if buffer:
                    current_time = asyncio.get_event_loop().time()
                    base_delay = config_manager.get_dynamic_delay()
                    
                    is_active_interactive = user_interactive_mode.get(user_id, False) or (len(buffer) > 0 and (current_time - last_send_time) >= base_delay)
                    is_time_to_send = (current_time - last_send_time) >= (base_delay * current_flood_multiplier)
                    
                    # ИСПРАВЛЕНО (v5.2.1): Если в буфере лежит остаток логов (статистика пинга) после Ctrl+C,
                    # мы выталкиваем её НЕМЕДЛЕННО, не затирая данные!
                    if is_time_to_send or is_active_interactive or "statistics" in "".join(buffer).lower():
                        raw_chunk = "".join(buffer)
                        buffer.clear()
                        
                        clean_chunk = clean_terminal_garbage(raw_chunk)
                        if clean_chunk:
                            await send_safe_message(api, user_id, clean_chunk, is_interactive=is_active_interactive)
                            last_send_time = asyncio.get_event_loop().time()
                
    except asyncio.CancelledError:
        was_cancelled = True
    except Exception as e:
        log_message("ERROR", f"Ошибка в фоновом чтении: {e}")
    finally:
        if was_cancelled:
            buffer.clear()
        elif buffer:
            await send_safe_message(api, user_id, clean_terminal_garbage("".join(buffer)), is_interactive=user_interactive_mode.get(user_id, False))
        log_message("INFO", f"Фоновое чтение для {user_id} завершено.")
