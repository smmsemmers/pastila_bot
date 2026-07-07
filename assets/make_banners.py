"""
Генератор широких баннеров для трёх ботов Pastila OS (assets/banner-*.png).
Запуск: python assets/make_banners.py
Стиль — «пастила», в пару к иконкам (assets/make_icons.py). Слева — кубик пастилы
с символом бота, справа — имя, роль и ключевая задача. Иерархия: ядро = Pastila Code.
"""
import os
import math
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 2
OW, OH = 1280, 600
W, H = OW * SS, OH * SS

_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_REG = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(cands, size):
    for p in cands:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def bold(size):
    return _font(_BOLD, size)


def reg(size):
    return _font(_REG, size)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


BG_TOP = (255, 214, 228)
BG_BOT = (255, 247, 237)
C_TITLE = (123, 44, 75)   # сливовый — общий для заголовков
C_SUB = (176, 92, 122)

# бот: (тело, верх, кант, чернила-акцент, тег-роли, заголовок, ключевая задача, функции)
BOTS = {
    "bridge": {
        "base": (166, 224, 196), "top": (214, 244, 226), "edge": (140, 205, 172),
        "ink": (30, 130, 96), "sym": "spark", "tag": "ЯДРО",
        "title": "Pastila Code", "handle": "@pastila_code_remote_bot",
        "key": "Главный ИИ-собеседник команды.",
        "func": "Думает, разбирает, делает: код, деплой,\nфайлы, экспорты, связки сервисов.",
    },
    "taskbot": {
        "base": (244, 166, 183), "top": (251, 214, 223), "edge": (231, 146, 167),
        "ink": (194, 37, 92), "sym": "check", "tag": "ОПЕРАТОР",
        "title": "PastilaTaskBot", "handle": "@PastilaTaskBot",
        "key": "Держит задачи и сроки.",
        "func": "Задачи, дедлайны, напоминания, таблица.\n/new · /list · голосом · файлы.",
    },
    "gpt": {
        "base": (176, 196, 244), "top": (214, 226, 251), "edge": (146, 170, 231),
        "ink": (54, 78, 168), "sym": "lens", "tag": "ИССЛЕДОВАТЕЛЬ",
        "title": "GPT Remote", "handle": "@pastila_gPT_remote_bot",
        "key": "Копает информацию в интернете.",
        "func": "Deep research и веб-поиск со ссылками, OCR.\n/research · /agent · /ocr.",
    },
}


def draw_symbol(d, cx, cy, s, ink, sym):
    if sym == "check":
        d.line([(cx - s * 0.34, cy + s * 0.04), (cx - s * 0.06, cy + s * 0.32),
                (cx + s * 0.40, cy - s * 0.28)], fill=ink,
               width=max(10 * SS, int(s * 0.16)), joint="curve")
    elif sym == "lens":
        r = s * 0.34
        lcx, lcy = cx - s * 0.10, cy - s * 0.12
        d.ellipse([lcx - r, lcy - r, lcx + r, lcy + r], outline=ink,
                  width=max(8 * SS, int(s * 0.12)))
        d.line([(lcx + r * 0.72, lcy + r * 0.72), (cx + s * 0.44, cy + s * 0.44)],
               fill=ink, width=max(10 * SS, int(s * 0.15)))
    elif sym == "spark":
        def star(ccx, ccy, R, r, rot=90):
            pts = []
            for i in range(8):
                ang = math.radians(rot + i * 45)
                rr = R if i % 2 == 0 else r
                pts.append((ccx + rr * math.cos(ang), ccy - rr * math.sin(ang)))
            return pts
        d.polygon(star(cx, cy, s * 0.54, s * 0.17), fill=ink)
        d.polygon(star(cx + s * 0.44, cy - s * 0.42, s * 0.17, s * 0.05), fill=ink)


def make(name, b):
    random.seed(hash(name) & 0xFFFF)
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, y / H) + (255,))
    # сахарная пудра
    dust = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dust)
    for _ in range(180):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.choice([2, 3, 3, 4]) * SS
        dd.ellipse([x, y, x + r, y + r], fill=(255, 255, 255, random.randint(45, 110)))
    img = Image.alpha_composite(img, dust)
    d = ImageDraw.Draw(img)

    # кубик пастилы слева
    cx, cy = int(W * 0.20), int(H * 0.52)
    s = int(H * 0.30)
    x0, y0, x1, y1 = cx - s, cy - s, cx + s, cy + s
    rad = int(s * 0.42)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0 + 8 * SS, y0 + 16 * SS, x1 + 8 * SS, y1 + 16 * SS], radius=rad,
        fill=(150, 70, 95, 90))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(12 * SS)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=b["base"],
                        outline=b["edge"], width=4 * SS)
    d.rounded_rectangle([x0 + s * 0.16, y0 + s * 0.16, x1 - s * 0.16, y0 + s * 0.66],
                        radius=int(rad * 0.7), fill=b["top"])
    for _ in range(int(s / 6)):
        px = random.randint(int(x0 + 14 * SS), int(x1 - 14 * SS))
        py = random.randint(int(y0 + 12 * SS), int(y0 + s * 0.6))
        d.ellipse([px, py, px + 3 * SS, py + 3 * SS], fill=(255, 255, 255, 235))
    draw_symbol(d, cx, cy, s, b["ink"], b["sym"])

    # текст справа
    tx = int(W * 0.40)
    ink = b["ink"]
    # тег роли — «пилюля»
    tagf = bold(30 * SS)
    tw = d.textbbox((0, 0), b["tag"], font=tagf)
    pill_w = (tw[2] - tw[0]) + 44 * SS
    pill_h = 58 * SS
    py0 = int(H * 0.16)
    d.rounded_rectangle([tx, py0, tx + pill_w, py0 + pill_h], radius=pill_h // 2,
                        fill=ink + (255,))
    d.text((tx + pill_w / 2, py0 + pill_h / 2), b["tag"], font=tagf,
           fill=(255, 255, 255, 255), anchor="mm")

    d.text((tx, int(H * 0.34)), b["title"], font=bold(84 * SS),
           fill=C_TITLE + (255,), anchor="lm")
    d.text((tx, int(H * 0.50)), b["key"], font=bold(40 * SS),
           fill=ink + (255,), anchor="lm")
    d.multiline_text((tx, int(H * 0.60)), b["func"], font=reg(30 * SS),
                     fill=C_SUB + (255,), spacing=10 * SS, anchor="la")
    d.text((tx, int(H * 0.86)), b["handle"], font=reg(28 * SS),
           fill=C_SUB + (255,), anchor="lm")

    out = img.convert("RGB").resize((OW, OH), Image.LANCZOS)
    path = os.path.join(HERE, f"banner-{name}.png")
    out.save(path, "PNG")
    print("saved", path, out.size)
    return path


if __name__ == "__main__":
    for n, b in BOTS.items():
        make(n, b)
