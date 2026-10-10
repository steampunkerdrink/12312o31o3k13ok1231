# -*- coding: utf-8 -*-
"""Рендер вывода команды в PNG (для команды «art:»).

Парсит ANSI/SGR и символы, собирает сетку клеток (char, fg, bg).
Если в выводе есть полублоки (▀▄█…) — рисует их как пиксели («терминальная
графика», например QR от `qrencode -t ansiutf8`).
Иначе — рисует текст моноширинным шрифтом на фоне терминала (например `ls -la`).
"""
import re
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# Покрытие ячейки символами-полублоками: индекс = строка*2 + колонка (сетка 2x2 подпикселя)
BLOCK_COVERAGE = {
    "█": {0, 1, 2, 3},
    "▀": {0, 1},
    "▄": {2, 3},
    "▌": {0, 2},
    "▐": {1, 3},
    "▘": {0},
    "▝": {1},
    "▖": {2},
    "▗": {3},
    "▞": {1, 2},
    "▚": {0, 3},
    "▛": {0, 1, 2},
    "▜": {0, 1, 3},
    "▙": {0, 2, 3},
    "▟": {1, 2, 3},
}
BLOCK_CHARS = set(BLOCK_COVERAGE)

# Стандартная xterm-палитра (0..15)
PALETTE = {
    0: (0, 0, 0), 1: (205, 49, 49), 2: (13, 188, 121), 3: (229, 229, 16),
    4: (36, 114, 200), 5: (188, 63, 188), 6: (17, 168, 205), 7: (229, 229, 229),
    8: (102, 102, 102), 9: (241, 76, 76), 10: (35, 209, 139), 11: (245, 245, 67),
    12: (59, 142, 234), 13: (214, 112, 214), 14: (41, 184, 219), 15: (255, 255, 255),
}

DEFAULT_FG = (235, 235, 235)
DEFAULT_BG = (18, 18, 24)

# Разбор: сначала SGR (…m), потом прочие CSI/ESC-последовательности, потом один любой символ
TOKEN = re.compile(r"\x1B\[([0-9;]*)m|\x1B\[[0-9;?]*[a-zA-Z]|\x1B[()][0-9A-Z]|[\s\S]")


def _color_256(idx: int):
    if idx < 16:
        return PALETTE.get(idx, DEFAULT_FG)
    if idx < 232:
        idx -= 16

        def v(x):
            return 0 if x == 0 else 55 + x * 40

        return (v(idx // 36), v((idx % 36) // 6), v(idx % 6))
    grey = 8 + (idx - 232) * 10
    return (grey, grey, grey)


class _Cell:
    __slots__ = ("char", "fg", "bg")

    def __init__(self, char, fg, bg):
        self.char = char
        self.fg = fg
        self.bg = bg


def _parse(text: str):
    """Разбор вывода в список строк; каждая строка — список _Cell."""
    fg, bg = DEFAULT_FG, DEFAULT_BG
    inverse = False
    rows, cur = [], []
    for m in TOKEN.finditer(text):
        grp = m.group(0)
        if grp.startswith("\x1b"):
            codes = m.group(1)
            if codes is not None:
                nums = [int(x) for x in codes.split(";") if x != ""] if codes else [0]
                i, n = 0, len(nums)
                while i < n:
                    c = nums[i]
                    if c == 0:
                        fg, bg, inverse = DEFAULT_FG, DEFAULT_BG, False
                    elif c == 7:
                        inverse = True
                    elif c == 27:
                        inverse = False
                    elif 30 <= c <= 37:
                        fg = PALETTE[c - 30]
                    elif 90 <= c <= 97:
                        fg = PALETTE[c - 90 + 8]
                    elif 40 <= c <= 47:
                        bg = PALETTE[c - 40]
                    elif 100 <= c <= 107:
                        bg = PALETTE[c - 100 + 8]
                    elif c == 39:
                        fg = DEFAULT_FG
                    elif c == 49:
                        bg = DEFAULT_BG
                    elif c in (38, 48):
                        kind = "fg" if c == 38 else "bg"
                        if i + 1 < n and nums[i + 1] == 5 and i + 2 < n:
                            color = _color_256(nums[i + 2])
                            if kind == "fg":
                                fg = color
                            else:
                                bg = color
                            i += 3
                            continue
                        if i + 1 < n and nums[i + 1] == 2 and i + 4 < n:
                            color = (nums[i + 2], nums[i + 3], nums[i + 4])
                            if kind == "fg":
                                fg = color
                            else:
                                bg = color
                            i += 5
                            continue
                    i += 1
            continue
        if grp == "\n":
            rows.append(cur)
            cur = []
            continue
        if grp == "\r":
            continue
        cur.append(_Cell(grp, fg if not inverse else bg, bg if not inverse else fg))
    if cur:
        rows.append(cur)
    return rows


def _cell_fill(ch: str):
    """Возвращает (top, bottom) — заполнение верхней/нижней половины ячейки.

    `qrencode -t ansiutf8` кладёт в одну текстовую ячейку ДВА модуля QR по вертикали
    (полублоки ▀/▄/█), поэтому ячейка = 1 модуль в ширину × 2 модуля в высоту.
    """
    if ch in (" ", "\t", ""):
        return (False, False)
    if ch == "▀":
        return (True, False)
    if ch == "▄":
        return (False, True)
    return (True, True)  # █ и прочие — заполняем полностью


def _render_art(rows, cell=12):
    ncols = max((len(r) for r in rows), default=0)
    nrows = len(rows)
    if ncols == 0 or nrows == 0:
        return Image.new("RGB", (4, 4), DEFAULT_BG)
    w, h = ncols * cell, nrows * 2 * cell
    img = Image.new("RGB", (w, h), DEFAULT_BG)
    draw = ImageDraw.Draw(img)
    y = 0
    for row in rows:
        x = 0
        for cellobj in row:
            top, bottom = _cell_fill(cellobj.char)
            draw.rectangle(
                [x, y, x + cell - 1, y + cell - 1],
                fill=cellobj.fg if top else cellobj.bg,
            )
            draw.rectangle(
                [x, y + cell, x + cell - 1, y + 2 * cell - 1],
                fill=cellobj.fg if bottom else cellobj.bg,
            )
            x += cell
        y += 2 * cell
    return img


def _load_font(size=17):
    for name in ("DejaVuSansMono.ttf", "consola.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render_text(rows):
    font = _load_font()
    cell_w = int(font.getlength("M")) or 10
    try:
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
    except Exception:
        line_h = int(cell_w * 1.4)
    ncols = max((len(r) for r in rows), default=0)
    nrows = len(rows)
    img = Image.new("RGB", (max(ncols * cell_w, 1), max(nrows * line_h, 1)), DEFAULT_BG)
    draw = ImageDraw.Draw(img)
    y = 0
    for row in rows:
        x = 0
        for cellobj in row:
            draw.rectangle([x, y, x + cell_w - 1, y + line_h - 1], fill=cellobj.bg)
            draw.text((x, y), cellobj.char, font=font, fill=cellobj.fg)
            x += cell_w
        y += line_h
    return img


def render(source) -> bytes:
    """Принимает bytes или str, возвращает PNG bytes."""
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    rows = _parse(text)
    is_art = any(c.char in BLOCK_CHARS for row in rows for c in row)
    img = _render_art(rows) if is_art else _render_text(rows)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()