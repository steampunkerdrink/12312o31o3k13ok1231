import asyncio
import getpass
import logging
import os
import re
import shutil
import signal
import time
try:
    import pty
    import fcntl
    import termios
    import struct
except ImportError:  # не Linux (напр. Windows)
    pty = None
    fcntl = termios = struct = None
from bot import config as config_manager
from bot import listener as shell_worker
from bot import render as terminal_render
from vkbottle.bot import Bot, Message

# Универсальная загрузка фото/документов: в vkbottle 4.7+ отдельные классы, раньше — MessageUploader
try:
    from vkbottle import PhotoMessageUploader as _PhotoUploader, DocMessagesUploader as _DocUploader
except ImportError:
    from vkbottle import MessageUploader as _PhotoUploader
    _DocUploader = None

_mirrored = False


def _mirror_logging_to_file() -> None:
    """Дублирует логи/ошибки vkbottle (модуль logging) в logs/info.log и logs/error.log.

    Раньше трейсбеки падали только в journalctl/systemd; здесь цепляем обработчики
    на корневой логгер: `>= ERROR` → error.log, остальное → info.log.
    """
    global _mirrored
    if _mirrored:
        return
    _mirrored = True
    try:
        root = logging.getLogger()
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        class _BelowError(logging.Filter):
            def filter(self, record):
                return record.levelno < logging.ERROR

        info_handler = logging.FileHandler(config_manager.INFO_LOG_FILE, encoding="utf-8")
        info_handler.setFormatter(fmt)
        info_handler.addFilter(_BelowError())

        err_handler = logging.FileHandler(config_manager.ERROR_LOG_FILE, encoding="utf-8")
        err_handler.setFormatter(fmt)
        err_handler.setLevel(logging.ERROR)

        root.addHandler(info_handler)
        root.addHandler(err_handler)
        root.setLevel(logging.INFO)
    except Exception as e:
        print(f"[mirror] Не удалось настроить файловый лог: {e}", flush=True)


_mirror_logging_to_file()

VERSION = config_manager.VERSION
START_TIME = time.time()
TOKEN, ALLOWED_ADMINS = config_manager.load_secret_config()

bot = Bot(token=TOKEN)
user_sessions = {}

# Блокировки отправки ART на пользователя: чтобы фото+документ одной команды не перемешивались
# с сообщениями следующей команды (и не приезжали в «чужом» ответе).
_art_send_locks = {}


def _art_lock(user_id: int) -> asyncio.Lock:
    lock = _art_send_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _art_send_locks[user_id] = lock
    return lock

# Фото-загрузчик: имя файла picture.png — так VK распознаёт PNG (рендер отдаёт PNG).
try:
    photo_uploader = _PhotoUploader(bot.api, attachment_name="picture.png")
except TypeError:
    photo_uploader = _PhotoUploader(bot.api)
# Фолбэк-загрузчик картинки как документа (имя задаём через title при загрузке).
try:
    doc_uploader = _DocUploader(bot.api, attachment_name="output.png") if _DocUploader is not None else None
except TypeError:
    doc_uploader = _DocUploader(bot.api) if _DocUploader is not None else None


async def _show_typing(user_id: int):
    """Статус «печатает…». Это НЕ сообщение в чат (messages.setActivity) — не спамит ВК."""
    try:
        await bot.api.messages.set_activity(type="typing", peer_id=user_id)
    except Exception:
        pass


async def _notify_startup():
    """Приветствие всем админам при запуске бота."""
    msg = f"🟢 Бот запущен [v{VERSION}]"
    for admin in ALLOWED_ADMINS:
        try:
            await bot.api.messages.send(peer_id=admin, message=msg, random_id=0)
        except Exception as e:
            config_manager.log_message("ERROR", f"Не удалось отправить приветствие {admin}: {e}")


def _close_session(user_id: int):
    """Полностью закрыть PTY-сессию пользователя (без отправки сообщений)."""
    session = user_sessions.pop(user_id, None)
    shell_worker.session_tokens.pop(user_id, None)
    shell_worker.last_activity.pop(user_id, None)
    if session:
        try:
            session["task"].cancel()
        except Exception:
            pass
        try:
            os.close(session["master_fd"])
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(session["process"].pid), signal.SIGKILL)
        except Exception:
            pass
    shell_worker.user_interactive_mode[user_id] = False


async def _teardown_session(user_id: int):
    """Асинхронно снимает сессию: отменяет и дожидается слушателя, убивает процесс.

    Дождаться завершения слушателя важно: иначе fd/транспорт старой сессии ещё не
    освобождены, и сразу созданная новая сессия окажется «глухой».
    """
    session = user_sessions.pop(user_id, None)
    shell_worker.session_tokens.pop(user_id, None)
    shell_worker.last_activity.pop(user_id, None)
    if not session:
        return
    try:
        session["task"].cancel()
        await asyncio.wait_for(session["task"], timeout=2)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass
    try:
        os.killpg(os.getpgid(session["process"].pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        os.close(session["master_fd"])
    except Exception:
        pass


async def _idle_watchdog():
    """Периодически закрывает простаивающие сессии. Порог — idle_timeout.txt (0 = выкл)."""
    while True:
        try:
            timeout = config_manager.get_idle_timeout()
            if timeout > 0:
                now = time.time()
                for uid in list(user_sessions.keys()):
                    last = shell_worker.last_activity.get(uid, now)
                    if now - last >= timeout:
                        config_manager.log_message("INFO", f"Авто-закрытие простаивающей сессии {uid} (простой > {timeout} c)")
                        _close_session(uid)
                        try:
                            await bot.api.messages.send(
                                peer_id=uid,
                                message=f"💤 Сессия закрыта из-за простоя (> {timeout} c).",
                                random_id=0,
                            )
                        except Exception:
                            pass
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка idle-watchdog: {e}")
        await asyncio.sleep(30)


async def _cleanup_sessions():
    """Аккуратное завершение: убить все дочерние PTY при остановке бота."""
    for uid in list(user_sessions.keys()):
        _close_session(uid)


async def _graceful_stop():
    config_manager.log_message("INFO", "Сигнал остановки: закрываю сессии и останавливаю задачи")
    await _cleanup_sessions()
    try:
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks(loop) if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            # Дожидаемся отмены, иначе asyncio ругается: "Task was destroyed but it is pending!"
            await asyncio.gather(*pending, return_exceptions=True)
        loop.stop()
    except Exception as e:
        config_manager.log_message("ERROR", f"Ошибка при остановке: {e}")


async def _install_signal_handlers():
    """SIGTERM/SIGINT → корректно закрыть сессии и остановить цикл (systemctl stop/restart)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_graceful_stop()))
        except (NotImplementedError, RuntimeError, ValueError):
            pass


# Регистрация в хуках vkbottle. ВАЖНО: это списки КОРУТИН (объектов), а не функций.
# on_startup/on_shutdown прогоняются через run_until_complete (короткие),
# startup_tasks — как долгоживущие задачи (_drain).
bot.on_startup.append(_notify_startup())
bot.on_startup.append(_install_signal_handlers())
bot.on_shutdown.append(_cleanup_sessions())
bot.startup_tasks.append(_idle_watchdog())

# Единый список команд внутреннего хард-ресета (используется в двух местах ниже)
HARD_RESET_COMMANDS = ["hard-reset", "убей сессию", "kill-session", "💀 hard reset"]

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m = seconds // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


def _read_proc_uptime():
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def _read_loadavg():
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            p = f.read().split()
        return f"{p[0]} {p[1]} {p[2]}"
    except Exception:
        return None


def _read_mem():
    total = avail = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                if total and avail:
                    break
        if total and avail:
            return total - avail, total
    except Exception:
        pass
    return None


def _read_cpu_times():
    """(total, idle) тиков CPU из /proc/stat. None при недоступности."""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
        return sum(parts), idle
    except Exception:
        return None


async def _cpu_percent(interval: float = 0.3):
    """Загрузка CPU в процентах: два замера /proc/stat с паузой."""
    first = _read_cpu_times()
    if not first:
        return None
    await asyncio.sleep(interval)
    second = _read_cpu_times()
    if not second:
        return None
    d_total = second[0] - first[0]
    d_idle = second[1] - first[1]
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))


async def build_status_text(user_id: int) -> str:
    """Собирает статус: бот + сессия + сервер. Каждая метрика — best-effort (н/д при сбое)."""
    lines = [f"📊 Статус бота [v{config_manager.get_version()}]"]

    # Бот
    try:
        up = _fmt_dur(time.time() - START_TIME)
    except Exception:
        up = "н/д"
    try:
        mode = "🖼 ART" if config_manager.art_mode else "📄 Текст"
    except Exception:
        mode = "н/д"
    try:
        kb = "вкл" if config_manager.is_keyboard_enabled() else "выкл"
    except Exception:
        kb = "н/д"
    try:
        delay = config_manager.get_dynamic_delay()
    except Exception:
        delay = "н/д"
    flood = shell_worker.current_flood_multiplier
    lines.append(f"🤖 Бот: uptime {up} | режим {mode} | клавиатура {kb} | задержка {delay}с | флуд ×{flood:.1f}")

    # Сессия
    sess = user_sessions.get(user_id)
    if sess and sess["process"].returncode is None:
        inter = "интерактивный" if shell_worker.user_interactive_mode.get(user_id, False) else "обычный"
        last = shell_worker.user_interactive_mode.get(f"{user_id}_last_cmd", "—")
        lines.append(
            f"🖥️ Сессия: активна (PID {sess['process'].pid}) | папка {shell_worker.last_valid_path} "
            f"| {inter} | последняя: {last}"
        )
    else:
        lines.append("🖥️ Сессия: не активна")

    # Сервер (всё best-effort)
    try:
        run_user = getpass.getuser()
    except Exception:
        run_user = "н/д"
    host = getattr(shell_worker, "HOSTNAME", "н/д")
    upt = _read_proc_uptime()
    server_up = _fmt_dur(upt) if upt else "н/д"
    load = _read_loadavg() or "н/д"
    mem = _read_mem()
    mem_s = f"{_fmt_bytes(mem[0])}/{_fmt_bytes(mem[1])}" if mem else "н/д"
    try:
        du = shutil.disk_usage("/")
        disk_s = f"{_fmt_bytes(du.used)}/{_fmt_bytes(du.total)}"
    except Exception:
        disk_s = "н/д"
    cores = os.cpu_count() or "н/д"
    cpu_pct = await _cpu_percent()
    cpu_s = f"{cpu_pct:.0f}%" if cpu_pct is not None else "н/д"
    lines.append(f"🌐 Сервер: {host} | user {run_user} | uptime {server_up}")
    lines.append(f"   CPU {cpu_s} ({cores} ядер) | load {load} | RAM {mem_s} | диск {disk_s}")
    return "\n".join(lines)


def _pty_preexec():
    """Дочерний процесс: новая сессия + назначить PTY управляющим терминалом.

    Без TIOCSCTTY интерактивные программы (ssh, htop, vim…) теряют controlling tty.
    """
    os.setsid()
    if fcntl is not None:
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except Exception:
            pass


async def _create_session(user_id: int, api):
    """Создаёт новую PTY-bash сессию и возвращает её (без health-проверки)."""
    config_manager.log_message("INFO", f"Запуск новой интерактивной bash-сессии для ID {user_id}")

    master_fd, slave_fd = os.openpty()
    # Размер окна PTY (иначе 0x0 — ломает ssh/htop/vim)
    if struct is not None:
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        except Exception:
            pass
    process = await asyncio.create_subprocess_exec(
        "/bin/bash", stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=_pty_preexec,
        env={**os.environ, "TERM": "xterm-256color"},
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

    token = object()
    shell_worker.session_tokens[user_id] = token
    listen_task = asyncio.create_task(
        shell_worker.start_output_listener(user_id, process_reader, api, process, token)
    )

    user_sessions[user_id] = {
        "process": process,
        "writer": writer,
        "task": listen_task,
        "master_fd": master_fd,
    }
    shell_worker.user_interactive_mode[user_id] = False

    await asyncio.sleep(0.3)
    # Инициализация сессии. PS1='' — убираем приглашение bash (иначе вывод без \n
    # слипается с промптом). Маркер здесь НЕ печатаем: иначе слушатель примет его
    # за завершение команды и пришлёт лишний заголовок пути (дубликат).
    # Алиасы НЕ хардкодим: сессия интерактивная, bash сам читает реальный ~/.bashrc.
    # Стартовую папку/инициализацию задаёт config/entrypoint.txt (v5.11.0).
    entry = config_manager.get_entrypoint()
    init_cmd = "stty -echo\nstty -icanon\nPS1=''\nPS2=''\n"
    if entry.get("workdir"):
        init_cmd += f"cd {entry['workdir']}\n"
        shell_worker.last_valid_path = entry["workdir"]
    if entry.get("init"):
        init_cmd += entry["init"] + "\n"
    writer.write(init_cmd.encode("utf-8"))
    await writer.drain()
    return user_sessions[user_id]


async def _session_responds(user_id: int, writer) -> bool:
    """Health-check: bash живой и реально читает наш fd? Побочный вывод зонда сбрасываем."""
    shell_worker.session_ready.discard(user_id)
    shell_worker.discard_output(user_id)
    try:
        writer.write(b"echo __SESS_READY__\n")
        await writer.drain()
    except Exception as e:
        config_manager.log_message("ERROR", f"health-check ID {user_id}: запись не удалась: {e}")
        shell_worker.discard_output(user_id)
        return False
    for _ in range(10):  # до ~2 секунд на ответ
        await asyncio.sleep(0.2)
        if user_id in shell_worker.session_ready:
            shell_worker.session_ready.discard(user_id)
            shell_worker.discard_output(user_id)
            return True
    shell_worker.discard_output(user_id)
    return False


async def get_or_create_session(user_id: int, api):
    """Возвращает активную сессию, при необходимости создаёт (с health-check и 1 повтором)."""
    if user_id in user_sessions:
        session = user_sessions[user_id]
        if session["process"].returncode is None:
            return session
    for attempt in range(2):
        await _create_session(user_id, api)
        if await _session_responds(user_id, user_sessions[user_id]["writer"]):
            return user_sessions[user_id]
        config_manager.log_message(
            "WARNING",
            f"ID {user_id}: сессия не ответила на health-check (попытка {attempt + 1}/2) — пересоздаю",
        )
        await _teardown_session(user_id)
        await asyncio.sleep(0.3)
    if user_id not in user_sessions:
        await _create_session(user_id, api)
    return user_sessions[user_id]


def _vk_error_text(e) -> str:
    """Читаемое описание ошибки VK (код + сообщение), иначе обычный текст исключения."""
    code = getattr(e, "code", None)
    msg = getattr(e, "error_msg", None) or getattr(e, "message", None)
    if code is not None and msg:
        return f"VK error {code}: {msg}"
    if msg:
        return str(msg)
    return str(e)


def _fail_message_with_tmp(user_id: int, data: bytes, reason: str) -> str:
    """Собирает текст ошибки и сохраняет картинку во временный файл tmp/art_<id>.png."""
    msg = f"Не удалось отправить картинку ({reason})."
    try:
        path = os.path.join(config_manager.TMP_DIR, f"art_{user_id}.png")
        with open(path, "wb") as f:
            f.write(data)
        msg += f"\n💾 Сохранил картинку на сервере: {path}"
        config_manager.log_message("INFO", f"Картинка сохранена в tmp: {path}")
    except Exception as e:
        config_manager.log_message("ERROR", f"Не удалось сохранить картинку в tmp: {e}")
    return msg


async def _send_image_to_user(user_id: int, image_bytes: bytes, caption: str = "", force_doc: bool = False):
    """Отправляет ART-картинку.

    По умолчанию — фото-превью (инлайн); документ уходит только если фото не прошло.
    При `force_doc=True` (команда `art:doc …`) сразу шлём документом.
    Если VK не принял ничего — кладём PNG во временный файл и сообщаем путь.
    """
    # ── Принудительно документом (art:doc) ──
    if force_doc and doc_uploader is not None:
        try:
            doc_attachment = await asyncio.wait_for(
                doc_uploader.upload(file_source=image_bytes, peer_id=user_id, title="art.png"), timeout=30
            )
            await bot.api.messages.send(peer_id=user_id, message=caption, attachment=doc_attachment, random_id=0)
            config_manager.log_message("INFO", f"Админ [{user_id}] art-картинка: документ (art:doc) отправлен")
            return
        except Exception as e_doc:
            config_manager.log_message("ERROR", f"ART art:doc: документ не прошёл: {_vk_error_text(e_doc)}")
            await bot.api.messages.send(
                peer_id=user_id,
                message=_fail_message_with_tmp(user_id, image_bytes, f"документ: {_vk_error_text(e_doc)}"),
                random_id=0,
            )
            return

    # ── 1) Фото-превью (PNG, с ретраями) ──
    photo_err = None
    if photo_uploader is not None:
        for _attempt in range(1, 4):
            try:
                if hasattr(photo_uploader, "upload"):
                    attachment = await asyncio.wait_for(
                        photo_uploader.upload(file_source=image_bytes, peer_id=user_id), timeout=30
                    )
                else:
                    attachment = await asyncio.wait_for(
                        photo_uploader.upload_photos(file_source=image_bytes, peer_id=user_id), timeout=30
                    )
                await bot.api.messages.send(peer_id=user_id, message=caption, attachment=attachment, random_id=0)
                config_manager.log_message("INFO", f"Админ [{user_id}] art-картинка: фото-превью отправлено")
                return
            except Exception as e_photo:
                photo_err = e_photo
                await asyncio.sleep(1)

    # ── 2) Фолбэк документом — ТОЛЬКО если фото не прошло ──
    if doc_uploader is not None:
        try:
            doc_attachment = await asyncio.wait_for(
                doc_uploader.upload(file_source=image_bytes, peer_id=user_id, title="art.png"), timeout=30
            )
            await bot.api.messages.send(
                peer_id=user_id,
                message=(caption + f"\n⚠️ Фото не прошло ({_vk_error_text(photo_err)}); отправил документом (PNG)").strip(),
                attachment=doc_attachment, random_id=0,
            )
            config_manager.log_message("INFO", f"Админ [{user_id}] art-картинка: документ (фолбэк) отправлен")
            return
        except Exception as e_doc:
            config_manager.log_message("ERROR", f"ART фото: {_vk_error_text(photo_err)}; документ: {_vk_error_text(e_doc)}")
            await bot.api.messages.send(
                peer_id=user_id,
                message=_fail_message_with_tmp(
                    user_id, image_bytes, f"фото: {_vk_error_text(photo_err)}; документ: {_vk_error_text(e_doc)}"
                ),
                random_id=0,
            )
            return

    await bot.api.messages.send(
        peer_id=user_id,
        message=_fail_message_with_tmp(user_id, image_bytes, f"фото: {_vk_error_text(photo_err)}"),
        random_id=0,
    )


async def _resend_last_art(user_id: int, message: Message):
    """Повторно отправляет последнюю ART-картинку из tmp/art_<id>.png (команда `art:last`)."""
    path = os.path.join(config_manager.TMP_DIR, f"art_{user_id}.png")
    if not os.path.exists(path):
        await message.answer("Нет сохранённой картинки — ещё не было art-вывода.")
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        config_manager.log_message("ERROR", f"Не удалось прочитать {path}: {e}")
        await message.answer(f"Не удалось прочитать сохранённую картинку: {e}")
        return
    await _show_typing(user_id)
    async with _art_lock(user_id):
        await _send_image_to_user(user_id, data, caption="🔁 Последняя ART-картинка")


# Лимит на скачиваемый ИСХОДНЫЙ файл (по URL): после скачивания идёт потоковая
# base64-кодировка и авто-разбиение на ~95MB части, поэтому файл может быть большим.
SEND_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 ГБ (потоково, в память не грузится)
# Если итоговый base64 .txt больше этой величины — разбиваем на части (лимит VK на
# документ ~200 МБ, оставляем запас; base64-текст безопасно доклеивается).
PART_MAX_BYTES = 95 * 1024 * 1024

# Команда send: файл кодируется в base64 и сохраняется как .txt — так VK принимает
# документ (txt не считается архивом), а содержимое скрыто кодированием (в т.ч. от
# VK-проверки архивов: empty_archive/wrong_arch_file). Пользователь декодирует у себя.

# Кэш group_id сообщества (для getMessagesUploadServer). Для ботов-сообществ без group_id
# upload-сервер не возвращает `file`, и docs.save падает с «file is undefined».
_GROUP_ID_CACHE: int | None = None


async def _get_group_id() -> int | None:
    """Возвращает ID группы-сообщества (для загрузчиков). Кэшируется после первого запроса."""
    global _GROUP_ID_CACHE
    if _GROUP_ID_CACHE is not None:
        return _GROUP_ID_CACHE
    try:
        resp = await bot.api.request("groups.getById", {})
        items = resp.get("response", {}).get("groups", []) if isinstance(resp, dict) else []
        if items:
            _GROUP_ID_CACHE = items[0].get("id")
            config_manager.log_message("INFO", f"_get_group_id: group_id={_GROUP_ID_CACHE}")
    except Exception as e:
        config_manager.log_message("ERROR", f"Не удалось получить group_id: {e}")
    return _GROUP_ID_CACHE


async def _upload_document(data: bytes, peer_id: int, title: str):
    """Загружает документ в чат (собственная реализация с полной диагностикой).

    Обходит `vkbottle.DocMessagesUploader`, чтобы видеть реальный ответ
    upload-сервера: при сбое `file is undefined` было непонятно, где именно
    теряется `file`. Здесь логируется каждый шаг: получение upload_url,
    HTTP-POST файла (сырой ответ сервера), затем docs.save.

    Возвращает (attachment_string, None) при успехе, иначе (None, сообщение об ошибке).
    """
    import json

    if doc_uploader is None:
        return None, "Загрузчик документов недоступен."

    group_id = await _get_group_id()

    # 1) Получаем upload_url для загрузки документа в диалог.
    try:
        up_params = {"peer_id": peer_id}
        if group_id is not None:
            up_params["group_id"] = group_id
        resp = await bot.api.request("docs.getMessagesUploadServer", up_params)
        upload_url = resp["response"]["upload_url"]
        config_manager.log_message(
            "INFO", f"getMessagesUploadServer OK (['upload_url']={upload_url[:80]}…)"
        )
    except Exception as e:
        err = _vk_error_text(e)
        config_manager.log_message(
            "ERROR",
            f"getMessagesUploadServer (peer_id={peer_id}, group_id={group_id}): {err}",
        )
        return None, f"Не удалось получить upload_url: {err}"

    # 2) POST файла (multipart, поле `file`) и чтение СЫРОГО ответа сервера.
    try:
        import aiohttp
        from io import BytesIO

        form = aiohttp.FormData()
        form.add_field("file", BytesIO(data), filename=title)
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(upload_url, data=form) as up_resp:
                raw_body = await up_resp.text()
        config_manager.log_message(
            "INFO", f"upload_url HTTP {up_resp.status}, ответ: {raw_body[:300]}"
        )
    except Exception as e:
        config_manager.log_message("ERROR", f"POST на upload_url не удался: {e}")
        return None, f"загрузка файла на upload-сервер не удалась: {e}"

    # 3) Разбираем ответ: ждём ключ `file`.
    try:
        upload_result = json.loads(raw_body)
    except Exception as e:
        config_manager.log_message("ERROR", f"upload-сервер вернул не-JSON: {e}: {raw_body[:300]}")
        return None, f"upload-сервер вернул не-JSON: {raw_body[:300]}"
    file_val = upload_result.get("file") if isinstance(upload_result, dict) else None
    if not file_val:
        config_manager.log_message(
            "ERROR", f"upload-сервер не вернул `file`; ответ: {raw_body[:500]}"
        )
        return None, f"upload-сервер не вернул `file` (ответ: {raw_body[:300]})"

    # 4) Сохраняем документ через docs.save.
    try:
        saved = await bot.api.request("docs.save", {"file": file_val, "title": title})
        doc = saved["response"]
        doc_type = doc["type"]
        item = doc[doc_type]
        owner = item["owner_id"]
        did = item["id"]
        key = item.get("access_key")
        return (f"{doc_type}{owner}_{did}_{key}" if key else f"{doc_type}{owner}_{did}"), None
    except Exception as e:
        err = _vk_error_text(e)
        config_manager.log_message(
            "ERROR",
            f"docs.save (title={title}, file_len={len(file_val)}): {err}",
        )
        return None, err


def _encode_b64_parts(src_path: str, user_id: int, display_name: str | None = None):
    """Потоково кодирует файл в base64 и пишет .txt-части в tmp/.

    - Файл читается блоками по 3 МБ (кратно 3 → base64 без потерь), в память целиком
      не грузится.
    - Если итоговый текст > PART_MAX_BYTES — разбивается на части `name.txt`,
      `name.part2.txt`, … Каждая часть — набор полных base64-строк (по 76 символов).
    - VK блокирует архивы (empty_archive/wrong_arch_file), а .txt принимает.
    - В имени нет исходного расширения (получатель не видит тип). Для URL-файлов,
      чей путь содержит служебный префикс dl_<id>_, можно передать display_name.

    Возвращает (parts, stem, None) при успехе, иначе ([], None, сообщение об ошибке).
    parts = [(abs_path, filename, byte_size), ...] в порядке следования.
    """
    import base64

    src_name = os.path.basename(src_path.rstrip("/\\")) if display_name is None else display_name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", src_name) or "file"
    # Убираем последнее расширение исходного файла (".exe" → ""): имя не выдаёт тип.
    stem = os.path.splitext(base)[0] or base

    parts: list = []
    f = None
    path = name = ""
    wbytes = 0
    buf = ""  # неполная строка (до 76 символов)
    CHUNK = 3 * 1024 * 1024  # кратно 3 байтам → base64 блочно без потерь
    with_error = None
    try:
        with open(src_path, "rb") as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                buf += base64.b64encode(chunk).decode("ascii")
                while len(buf) >= 76:
                    line, buf = buf[:76], buf[76:]
                    # Новая часть, когда текущая уже набрала предельный объём.
                    if f is None or (wbytes >= PART_MAX_BYTES and wbytes > 0):
                        if f is not None:
                            f.close()
                        name = f"{stem}.txt" if len(parts) == 0 else f"{stem}.part{len(parts)+1}.txt"
                        path = os.path.join(config_manager.TMP_DIR, f"send_{user_id}_{int(time.time())}_{name}")
                        f = open(path, "w", encoding="ascii")
                        parts.append([path, name, 0])
                        wbytes = 0
                    f.write(line + "\n")
                    wbytes += 77
                    parts[-1][2] = wbytes
            # Остаток последней строки (если объём не делится на 76 без остатка).
            if buf:
                if f is None:
                    name = f"{stem}.txt"
                    path = os.path.join(config_manager.TMP_DIR, f"send_{user_id}_{int(time.time())}_{name}")
                    f = open(path, "w", encoding="ascii")
                    parts.append([path, name, 0])
                    wbytes = 0
                f.write(buf + "\n")
                parts[-1][2] += len(buf) + 1
    except Exception as e:
        with_error = f"base64-кодирование не удалось: {e}"
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    if with_error is not None:
        return [], None, with_error
    # Возвращаем список (abs_path, name, byte_size).
    return [(p[0], p[1], p[2]) for p in parts], stem, None


async def _download_url_to_tmp(url: str, user_id: int):
    """Скачивает файл по URL во временный файл. Возвращает (path, filename) или (None, ошибка)."""
    try:
        import aiohttp
    except Exception as e:
        return None, f"aiohttp недоступен: {e}"
    from urllib.parse import urlparse, unquote

    timeout = aiohttp.ClientTimeout(total=600, connect=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None, f"HTTP {resp.status}"
                # Имя файла: из Content-Disposition, иначе из URI
                filename = ""
                cd = resp.headers.get("Content-Disposition", "")
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
                if m:
                    filename = m.group(1).strip()
                if not filename:
                    filename = unquote(os.path.basename(urlparse(url).path))
                filename = os.path.basename(filename.replace("\\", "/")).strip() or "download.bin"
                path = os.path.join(config_manager.TMP_DIR, f"dl_{user_id}_{filename}")
                total = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > SEND_MAX_BYTES:
                            try:
                                os.remove(path)
                            except Exception:
                                pass
                            return None, f"файл больше {_fmt_bytes(SEND_MAX_BYTES)}"
                        f.write(chunk)
                return path, filename
    except Exception as e:
        return None, str(e)


async def _send_file_command(user_id: int, text: str, message: Message):
    """`send <путь|url>` — прислать файл или папку (с сервера или по ссылке) в этот чат.

    Файл кодируется в base64 и отправляется как .txt (VK блокирует архивы с
    непроверяемым содержимым — empty_archive/wrong_arch_file, а .txt принимает).
    Получатель декодирует base64 обратно в файл.
    """
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи файл или ссылку: send <путь|url>  (напр.: send 1.txt  или  send https://host/f.zip)")
        return
    raw_arg = parts[1].strip().strip('"').strip("'")

    display_name = None

    if raw_arg.lower().startswith(("http://", "https://")):
        await message.answer(f"⏬ Скачиваю: {raw_arg}")
        path, info = await _download_url_to_tmp(raw_arg, user_id)
        if path is None:
            await message.answer(f"Не удалось скачать файл: {info}")
            return
        # Для URL используем исходное имя (путь содержит служебный префикс dl_<id>_).
        display_name = info if isinstance(info, str) else None
    else:
        base = shell_worker.last_valid_path or "/"
        path = os.path.expanduser(raw_arg if os.path.isabs(raw_arg) else os.path.join(base, raw_arg))
        if not os.path.exists(path):
            await message.answer(f"Файл не найден: {path}")
            return

    # ── Кодирование в base64 (.txt) — потоково, с разбиением на части ──
    await message.answer("🔒 Кодирую в base64 (.txt)…")
    parts, stem, b64_err = _encode_b64_parts(path, user_id, display_name)
    if not parts:
        await message.answer(f"Не удалось закодировать файл: {b64_err}")
        return

    if doc_uploader is None:
        await message.answer("Загрузчик документов недоступен.")
        return

    # Загружаем каждую .txt-часть как отдельный документ.
    attachments = []
    for p_path, p_name, _p_size in parts:
        await _show_typing(user_id)
        try:
            with open(p_path, "rb") as f:
                data = f.read()
        except Exception as e:
            config_manager.log_message("ERROR", f"send: чтение файла {p_path} не удалось: {e}")
            await message.answer(f"Не удалось прочитать файл {p_name}: {e}")
            return
        attachment, up_err = await _upload_document(data, user_id, p_name)
        if attachment is None:
            await message.answer(
                f"❌ VK не принял файл «{p_name}»:\n{up_err}\n\nФайл лежит на сервере: {p_path}"
            )
            return
        attachments.append(attachment)

    # Итоговое сообщение: имя + инструкция по склейке/декоду.
    names = [p[1] for p in parts]
    total_text = sum(p[2] for p in parts)
    if len(parts) == 1:
        head = f"🔑 {names[0]} ({_fmt_bytes(total_text)}) — это base64 (имя без расширения)."
        tip = (
            f"Linux:   base64 -d \"{names[0]}\" > \"{stem}\"\n"
            f"Windows: certutil -decode \"{names[0]}\" out.file\n"
        )
    else:
        head = (
            f"🔑 Файл разбит на {len(parts)} частей по ~95 МБ "
            f"(итого {_fmt_bytes(total_text)}): {', '.join(names)}"
        )
        order = " ".join(f'"{n}"' for n in names)          # cat a b
        order_win = "+".join(f'"{n}"' for n in names)      # copy /b a+b
        tip = (
            f"Склей части (порядок важен!), затем декодируй:\n"
            f"Linux:   cat {order} | base64 -d > \"{stem}\"\n"
            f"Windows: copy /b {order_win} out.b64\n"
            f"         certutil -decode out.b64 \"{stem}\"\n"
        )
    try:
        await bot.api.messages.send(
            peer_id=user_id,
            message=f"{head}\n«Скачай части, затем:\n{tip}",
            attachment=attachments, random_id=0,
        )
        config_manager.log_message(
            "INFO", f"Админ [{user_id}] send: {path} -> {len(parts)} txt-части ({_fmt_bytes(total_text)})"
        )
    except Exception as e:
        err = _vk_error_text(e)
        config_manager.log_message("ERROR", f"send «{stem}»: VK не отправил файл — {err}")
        await message.answer(f"❌ Не удалось отправить файл «{stem}»:\n{err}")


# Последняя art-команда пользователя — для подписи на картинке
_last_art_cmd = {}
# Флаг «прислать документом» на текущий art-запрос (команда `art:doc`)
_art_force_doc = {}


async def _render_session_image(user_id: int, raw_bytes: bytes):
    """Callback из shell_worker: рендерит сырой вывод сессии в картинку и шлёт в VK."""
    run_user = getattr(shell_worker, "RUN_USER", "root")
    host = getattr(shell_worker, "HOSTNAME", "host")
    path = shell_worker.last_valid_path or "/"
    prompt = "$" if run_user != "root" else "#"
    cmd = _last_art_cmd.get(user_id, "")
    # Промпт-заголовок рисуем прямо НА картинке (первой строкой), а не подписью в VK:
    # зелёный user@host, синий путь, затем «#/$» и команда. Шапка — отдельным текстовым
    # слоем, поэтому блочная графика (QR и т.п.) внизу не портится.
    header = f"\x1b[1;32m{run_user}@{host}\x1b[0m:\x1b[1;34m{path}\x1b[0m{prompt} {cmd}"
    try:
        # Рендер сразу отдаёт PNG — отправляем его как есть (без потерь, чёткий текст/QR).
        upload_bytes = terminal_render.render_stacked(header, raw_bytes or b"")
    except Exception as e:
        config_manager.log_message("ERROR", f"Ошибка рендера изображения из сессии: {e}")
        await bot.api.messages.send(peer_id=user_id, message=f"Не удалось отрисовать картинку: {e}", random_id=0)
        return
    # Сохраняем последнюю картинку — её повторно пришлёт команда `art:last`.
    try:
        with open(os.path.join(config_manager.TMP_DIR, f"art_{user_id}.png"), "wb") as f:
            f.write(upload_bytes)
    except Exception as e:
        config_manager.log_message("WARNING", f"Не удалось сохранить последнюю картинку: {e}")
    config_manager.log_message("INFO", f"Админ [{user_id}] art-рендер: {cmd or '(без команды)'}")
    force_doc = _art_force_doc.get(user_id, False)
    # Отправляем под блокировкой, чтобы фото+документ ушли цельно и не перемешались с другой командой
    async with _art_lock(user_id):
        await _send_image_to_user(user_id, upload_bytes, force_doc=force_doc)


shell_worker.image_renderer = _render_session_image


async def _capture_timeout(user_id: int, seconds: int = 60):
    """Страховка: если маркер не пришёл — снимаем захват и сообщаем."""
    await asyncio.sleep(seconds)
    if shell_worker.session_capture_requests.get(user_id):
        shell_worker.session_capture_requests.pop(user_id, None)
        shell_worker.session_capture_buf.pop(user_id, None)
        try:
            await bot.api.messages.send(
                peer_id=user_id,
                message=f"⏱ ART-захват: маркер не получен за {seconds} c (команда не завершилась?).",
                random_id=0,
            )
        except Exception:
            pass


async def _art_via_session(user_id: int, command: str):
    """ART в контексте активной сессии (в т.ч. ssh): пишем команду+маркер, рендер по маркеру."""
    session = await get_or_create_session(user_id, bot.api)
    writer = session["writer"]
    shell_worker.session_capture_buf[user_id] = bytearray()
    shell_worker.session_capture_requests[user_id] = True
    marker_cmd = "printf '\\n___END_OF_COMMAND___:%s:%s\\n' \"$?\" \"$PWD\""
    writer.write((command + "\n" + marker_cmd + "\n").encode("utf-8"))
    await writer.drain()
    asyncio.create_task(_capture_timeout(user_id))


async def run_as_art(user_id: int, command: str, message: Message, force_doc: bool = False):
    """ART: выполняет команду в контексте активной сессии (в т.ч. ssh) и шлёт картинку.

    force_doc=True — прислать результат документом (команда `art:doc`).
    """
    if not command:
        await message.answer("Пустая команда. Пример: art: ls -la")
        return
    config_manager.log_message("INFO", f"Админ [{user_id}] art-выполнение: {command}")
    await _show_typing(user_id)
    _last_art_cmd[user_id] = command
    _art_force_doc[user_id] = force_doc
    await _art_via_session(user_id, command)


HELP_TEXT = (
    "• обычная команда — выполняется в живой PTY-сессии\n"
    "• art: <команда> — результат картинкой (с цветами)\n"
    "• art:bg dark|light|auto — фон картинки · art:font N — размер шрифта\n"
    "• art:last — повторно прислать последнюю картинку\n"
    "• art:doc <команда> — прислать результат документом (по умолчанию — фото)\n"
    "• bg: <команда> — фоновый запуск (вывод приходит по мере работы)\n"
    "• send <файл|url> — прислать файл/папку с сервера или по ссылке (7z с паролем)\n"
    "• reset / сброс / exit — очистить сессию\n"
    "• 💀 Hard Reset — убить зависшую сессию\n"
    "• ctrl / ctrl+c — отправить SIGINT (прервать вывод)\n"
    "• status / статус — диагностика бота/сессии/сервера\n"
    "• 🖼 ART — все команды картинкой · 📄 Текст — вернуть текст"
)


async def handle_art_command(user_id: int, text: str, message: Message):
    """Обработчик префикса «art:» — картинка; есть и команды настройки рендера."""
    cmd = text.split(":", 1)[1].strip()
    low = cmd.strip().lower()

    # art:last — повторно прислать последнюю сохранённую картинку
    if low == "last":
        await _resend_last_art(user_id, message)
        return

    # art:doc <команда> — выполнить и прислать результат документом (а не фото)
    if low == "doc" or low.startswith("doc "):
        parts = cmd.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Пример: art:doc ls -la")
            return
        await run_as_art(user_id, parts[1].strip(), message, force_doc=True)
        return

    # Настройки рендера (не выполняются как команды в сессии)
    if low in ("light", "bg light", "bg white", "white"):
        terminal_render.configure(bg="light")
        await message.answer("🖼 Фон art-картинки: светлый. Вернуть тёмный: art:bg dark")
        return
    if low in ("dark", "bg dark", "bg auto", "auto"):
        terminal_render.configure(bg="dark")
        await message.answer("🖼 Фон art-картинки: тёмный. Светлый: art:bg light")
        return
    if low == "bg":
        await message.answer(f"🖼 Текущий фон art: {terminal_render.CONFIG['bg']} (art:bg dark|light|auto)")
        return
    if low == "font" or low.startswith("font "):
        parts = low.split()
        if len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
            terminal_render.configure(font_size=n)
            await message.answer(f"🔠 Размер шрифта art: {n} (текущий {terminal_render.CONFIG['font_size']})")
        else:
            await message.answer(f"🔠 Текущий размер шрифта art: {terminal_render.CONFIG['font_size']}. Пример: art:font 20")
        return

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

    # СТАТУС: диагностика бота, сессии и сервера
    if lower_text in ["status", "статус", "📊 статус"]:
        try:
            await message.answer(await build_status_text(user_id))
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка статуса: {e}")
            await message.answer(f"Ошибка статуса: {e}")
        return

    # HELP
    if lower_text in ["help", "помощь", "?", "команды", "commands", "хелп"]:
        await message.answer(f"📚 Команды бота [v{config_manager.get_version()}]\n" + HELP_TEXT)
        return

    # SEND <файл> — прислать файл с сервера в этот чат
    if lower_text == "send" or lower_text.startswith("send "):
        try:
            await _send_file_command(user_id, text_input, message)
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка send: {e}")
            await message.answer(f"Ошибка отправки файла: {e}")
        return

    # ART-РЕЖИМ: выполнить команду и прислать результат картинкой (PNG)
    if text_input.lower().startswith("art:"):
        try:
            await handle_art_command(user_id, text_input, message)
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка art-обработки: {e}")
            await message.answer(f"Ошибка при выполнении: {e}")
        return

    # ФОНОВЫЙ ЗАПУСК: bg:<команда> — не держит чат, вывод приходит по мере работы
    if text_input.lower().startswith("bg:"):
        bg_cmd = text_input.split(":", 1)[1].strip()
        if not bg_cmd:
            await message.answer("Пустая команда. Пример: bg: apt update")
            return
        try:
            session = await get_or_create_session(user_id, bot.api)
            session["writer"].write((f"( {bg_cmd} ) &\n").encode("utf-8"))
            await session["writer"].drain()
            config_manager.log_message("INFO", f"Админ [{user_id}] bg-запуск: {bg_cmd}")
            await message.answer(f"▶️ Фон: `{bg_cmd}`. Вывод появится в чате по мере работы.")
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка bg-запуска: {e}")
            await message.answer(f"❌ Ошибка фонового запуска: {e}")
        return

    current_kb = config_manager.generate_dynamic_keyboard()

    # СТАНДАРТНЫЙ СБРОС (v5.3.0)
    # Исключаем хард-ресет: иначе подстрока "reset" в "💀 hard reset" (текст кнопки
    # Hard Reset) срабатывала бы обычным сбросом раньше, чем внутренний хард-ресет.
    lower_text = text_input.lower()
    is_hard_reset = lower_text in HARD_RESET_COMMANDS
    if not is_hard_reset and ("сброс" in lower_text or "reset" in lower_text or lower_text == "exit"):
        await _teardown_session(user_id)
        shell_worker.user_interactive_mode[user_id] = False
        shell_worker.current_flood_multiplier = 1.0
        shell_worker.last_valid_path = "/root"
        current_ver = config_manager.get_version()
        await bot.api.messages.send(peer_id=user_id, message=f"🔄 Сессия очищена, задержки сброшены. [v{current_ver}]", keyboard=current_kb, random_id=0)
        return

    # ВНУТРЕННИЙ ХАРД-РЕСЕТ СЕССИИ (v5.3.0)
    if text_input.lower() in HARD_RESET_COMMANDS:
        config_manager.log_message("WARNING", f"Админ [{user_id}] HARD-RESET (kill-session): уничтожение сессии")
        await _teardown_session(user_id)
        shell_worker.user_interactive_mode[user_id] = False
        shell_worker.current_flood_multiplier = 1.0
        shell_worker.last_valid_path = "/root"
        config_manager.log_message("INFO", f"Админ [{user_id}] HARD-RESET: сессия уничтожена, ждём создание по следующей команде")

        # ИСПРАВЛЕНО (v5.9.2): НЕ создаём новую сессию тут же после уничтожения старой.
        # Раньше после HARD-RESET на тяжёлом ssh/интерактиве ОС не успевала освободить
        # fd/процессы, и новая сессия получалась «глухой» (молчала). Теперь создание ленивое —
        # как в обычном reset (этот путь стабилен): новую сессию поднимет следующая команда.
        await message.answer("💀 [INTERNAL HARD-RESET]: Сессия bash уничтожена. Новая создастся автоматически по следующей команде.")
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
    shell_worker.last_activity[user_id] = time.time()
    config_manager.log_message("INFO", f"Админ [{user_id}] отправил: {command}")

    # Если включён ART-режим — обычные команды шлём картинкой.
    # Исключение: интерактивные команды (ssh, apt, nano, mysql, ufw…) всё равно
    # идут в живую PTY-сессию, чтобы работать интерактивно (ART-захват
    # рассчитан на завершающиеся команды; у него страховка-таймаут 60 c).
    if config_manager.art_mode and not config_manager.is_command_interactive(command):
        try:
            await run_as_art(user_id, command, message)
        except Exception as e:
            config_manager.log_message("ERROR", f"Ошибка art-обработки: {e}")
            await message.answer(f"Ошибка при выполнении: {e}")
        return

    session_data = await get_or_create_session(user_id, bot.api)
    writer = session_data["writer"]

    await _show_typing(user_id)
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
                await message.answer(
                    "▶️ Интерактивный режим: команда выполняется в живой сессии. "
                    "Выход — «reset» или Ctrl+C."
                )
            else:
                full_command = (
                    command
                    + "\n__ec=$?\n"
                    + "echo ''\n"
                    + "printf '___END_OF_COMMAND___:%s:%s\\n' \"$__ec\" \"$PWD\"\n"
                )

            writer.write(full_command.encode("utf-8"))
            await writer.drain()
            
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки команды: {str(e)}")

def main():
    """Точка входа бота (запускается из run.py или `python -m bot.app`)."""
    current_ver = config_manager.get_version()
    config_manager.log_message("INFO", f"Запуск модульного безопасного СТРИМ-CLI-бота (v{VERSION})...")
    bot.run()


if __name__ == "__main__":
    main()
