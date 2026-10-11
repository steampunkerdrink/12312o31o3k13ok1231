import asyncio
import getpass
import re
import socket
import time
import config_manager
from config_manager import log_message

VERSION = config_manager.VERSION
MARKER = "___END_OF_COMMAND___"
user_interactive_mode = {}
current_flood_multiplier = 1.0

# Токен активного слушателя на пользователя. При reset/hard-reset меняется/снимается,
# чтобы «старый» фоновый слушатель перестал слать вывод после сброса.
session_tokens = {}

# ИСПРАВЛЕНО (v5.2.1): Кэш путей теперь монолитно живёт здесь, решая проблему Circular Import!
last_valid_path = "/root"

# Имя хоста для отображения промпта (не хардкодим конкретный сервер)
HOSTNAME = socket.gethostname()

# Реальный пользователь, под которым работает бот (не хардкодим root)
try:
    RUN_USER = getpass.getuser()
except Exception:
    RUN_USER = "user"

# Вкрапление shell-промпта (user@host:path#/$) — удаляем только его, сохраняя полезный
# вывод (важно, когда команда печатает текст без \n и промпт слипается с ним).
PROMPT_SUB = re.compile(r"[^\s@]+@[^\s:]+:[^\s]*[#$]\s?")

# Активность сессии (последнее событие) — для авто-закрытия простаивающих сессий
last_activity = {}

# Синтаксически корректный Regex для удаления ANSI-кодов цвета
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9;?]*[a-zA-Z])')

def clean_terminal_garbage(text: str) -> str:
    """Очищает вывод от ANSI-кодов и скрытых системных хвостов PTY"""
    text = ANSI_ESCAPE.sub('', text)
    text = text.replace("[?2004h", "").replace("[?2004l", "")
    
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if "stty " in line:
            continue
        if MARKER in line:
            line = line[:line.index(MARKER)]
        line = PROMPT_SUB.sub("", line)
        if not line.strip():
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

async def send_safe_message(api, peer_id: int, text: str, is_interactive: bool = False):
    """Нарезает длинный текст на куски и шлет в ВК порциями с адаптивным таймаутом"""
    global current_flood_multiplier
    if not text.strip(): return
    last_activity[peer_id] = time.time()
    
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
                    log_message("ERROR", f"Не-флуд ошибка отправки сообщения для {peer_id}: {e}")
                    await asyncio.sleep(2.0)
                    break

async def start_output_listener(user_id: int, process_reader, api, process, token=None):
    """Фоновый слушатель логов v5.2.1 (Исправлено сохранение путей и выгрузка статистики)"""
    global last_valid_path
    log_message("INFO", f"Запущено фоновое чтение PTY для пользователя {user_id} (v{VERSION})")

    def _is_alive():
        # Сессия актуальна, только если её токен всё ещё закреплён за user_id.
        # После reset/hard-reset токен меняется — старый слушатель больше не шлёт.
        return token is None or session_tokens.get(user_id) is token

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
                    exit_code = "0"
                    try:
                        clean_marker_line = ANSI_ESCAPE.sub('', decoded_chunk).strip()
                        if MARKER in clean_marker_line:
                            idx = clean_marker_line.index(MARKER)
                            # ВАЖНО: не схлопываем переводы строк — иначе, когда весь вывод
                            # команды приходит одним куском с маркером, многострочный вывод
                            # склеивается в одну строку.
                            leftover = clean_marker_line[:idx].replace("\r", "").rstrip()
                            if leftover and leftover.strip() != "stty -echo":
                                buffer.append(leftover + "\n")

                            # Формат: MARKER:<exit_code>:<pwd>
                            raw_rest = clean_marker_line[idx + len(MARKER):].replace("\r", "").replace("\n", "").strip()
                            raw_rest = raw_rest.lstrip(":")
                            parts = raw_rest.split(":", 1)
                            exit_code = parts[0].strip() if parts else "0"
                            current_path = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "/"

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
                    prompt_char = "#" if RUN_USER == "root" else "$"
                    ec_suffix = "" if exit_code in ("", "0") else f" ⚠️ exit {exit_code}"
                    path_header = f"📁 [v{fresh_ver}] {RUN_USER}@{HOSTNAME}:{current_path}{prompt_char} {executed_command}{ec_suffix}"

                    if not _is_alive():
                        return
                    last_activity[user_id] = time.time()
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
                        if clean_chunk and _is_alive():
                            await send_safe_message(api, user_id, clean_chunk, is_interactive=is_active_interactive)
                            last_send_time = asyncio.get_event_loop().time()
                
    except asyncio.CancelledError:
        was_cancelled = True
    except Exception as e:
        log_message("ERROR", f"Ошибка в фоновом чтении: {e}")
    finally:
        if was_cancelled:
            buffer.clear()
        elif buffer and _is_alive():
            await send_safe_message(api, user_id, clean_terminal_garbage("".join(buffer)), is_interactive=user_interactive_mode.get(user_id, False))
        log_message("INFO", f"Фоновое чтение для {user_id} завершено.")
