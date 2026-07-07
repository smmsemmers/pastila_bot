"""
Генератор квадратных иконок-аватаров для трёх ботов Pastila OS.
Запуск: python assets/make_icons.py
Стиль — «пастила»: пастельный фон + кубик пастилы с сахарной обсыпкой и символом.
Каждый бот — свой цвет и знак. Рисуем в 2x и уменьшаем (supersample) для гладких краёв.
Эмодзи не используем (PIL рисует их ч/б) — только векторные фигуры и текст.
"""
import os
import math
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 2                      # supersampling
OUT = 512
W = H = OUT * SS

# Шрифты: сначала macOS (Arial), потом Linux (DejaVu) — чтобы работало и на Маке, и на Render.
_FONT_CANDS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def bold(size):
    for p in _FONT_CANDS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# палитра «пастила»
BG_TOP = (255, 214, 228)
BG_BOT = (255, 247, 237)
SUGAR = (255, 255, 255)

# тема на каждого бота: (тело пастилы, верхняя грань, кант, цвет символа)
THEMES = {
    "taskbot": {
        "base": (244, 166, 183), "top": (251, 214, 223),
        "edge": (231, 146, 167), "ink": (194, 37, 92), "sym": "check",
    },
    "gpt": {
        "base": (176, 196, 244), "top": (214, 226, 251),
        "edge": (146, 170, 231), "ink": (54, 78, 168), "sym": "lens",
    },
    "bridge": {
        "base": (166, 224, 196), "top": (214, 244, 226),
        "edge": (140, 205, 172), "ink": (30, 130, 96), "sym": "spark",
    },
}


def make(name, theme):
    random.seed(hash(name) & 0xFFFF)
    base = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(base)
    # фон — мягкий градиент
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, y / H) + (255,))
    # сахарная пудра
    dust = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dust)
    for _ in range(120):
        x, y = random.randint(0, W), random.randint(0, H)
        r = random.choice([2, 3, 3, 4]) * SS
        dd.ellipse([x, y, x + r, y + r], fill=(255, 255, 255, random.randint(50, 120)))
    base = Image.alpha_composite(base, dust)
    d = ImageDraw.Draw(base)

    # кубик пастилы по центру
    cx, cy = W / 2, H / 2
    s = int(W * 0.30)
    x0, y0, x1, y1 = cx - s, cy - s, cx + s, cy + s
    rad = int(s * 0.42)
    # тень
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0 + 8 * SS, y0 + 16 * SS, x1 + 8 * SS, y1 + 16 * SS],
        radius=rad, fill=(150, 70, 95, 90))
    base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(12 * SS)))
    d = ImageDraw.Draw(base)
    # тело
    d.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=theme["base"],
                        outline=theme["edge"], width=4 * SS)
    # «сахарная» верхняя грань
    d.rounded_rectangle([x0 + s * 0.16, y0 + s * 0.16, x1 - s * 0.16, y0 + s * 0.66],
                        radius=int(rad * 0.7), fill=theme["top"])
    # обсыпка
    for _ in range(int(s / 6)):
        px = random.randint(int(x0 + 14 * SS), int(x1 - 14 * SS))
        py = random.randint(int(y0 + 12 * SS), int(y0 + s * 0.6))
        r = 3 * SS
        d.ellipse([px, py, px + r, py + r], fill=(255, 255, 255, 235))

    ink = theme["ink"]
    sym = theme["sym"]
    if sym == "check":
        w = max(10 * SS, int(s * 0.16))
        d.line([(cx - s * 0.34, cy + s * 0.04),
                (cx - s * 0.06, cy + s * 0.32),
                (cx + s * 0.40, cy - s * 0.28)],
               fill=ink, width=w, joint="curve")
    elif sym == "chat":
        # речевое облачко + три точки
        bx0, by0, bx1, by1 = cx - s * 0.42, cy - s * 0.34, cx + s * 0.42, cy + s * 0.18
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(s * 0.24),
                            outline=ink, width=max(7 * SS, int(s * 0.10)))
        d.polygon([(cx - s * 0.10, by1 - 2 * SS),
                   (cx - s * 0.30, cy + s * 0.44),
                   (cx + s * 0.06, by1 - 2 * SS)], fill=ink)
        r = int(s * 0.06)
        for dx in (-s * 0.20, 0, s * 0.20):
            ccx = cx + dx
            ccy = (by0 + by1) / 2
            d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], fill=ink)
    elif sym == "code":
        # символ </>
        f = bold(int(s * 0.95))
        d.text((cx, cy - s * 0.06), "</>", font=f, fill=ink, anchor="mm")
    elif sym == "lens":
        # лупа — исследователь / deep research
        r = s * 0.34
        lcx, lcy = cx - s * 0.10, cy - s * 0.12
        d.ellipse([lcx - r, lcy - r, lcx + r, lcy + r],
                  outline=ink, width=max(8 * SS, int(s * 0.12)))
        # ручка
        hx0, hy0 = lcx + r * 0.72, lcy + r * 0.72
        hx1, hy1 = cx + s * 0.44, cy + s * 0.44
        d.line([(hx0, hy0), (hx1, hy1)], fill=ink,
               width=max(10 * SS, int(s * 0.15)))

    elif sym == "spark":
        # искра/звезда — «умное ядро», главный ИИ
        def star(ccx, ccy, R, r, rot=90):
            pts = []
            for i in range(8):
                ang = math.radians(rot + i * 45)
                rr = R if i % 2 == 0 else r
                pts.append((ccx + rr * math.cos(ang), ccy - rr * math.sin(ang)))
            return pts
        d.polygon(star(cx, cy, s * 0.54, s * 0.17), fill=ink)
        d.polygon(star(cx + s * 0.44, cy - s * 0.42, s * 0.17, s * 0.05), fill=ink)

    out = base.convert("RGB").resize((OUT, OUT), Image.LANCZOS)
    path = os.path.join(HERE, f"icon-{name}.png")
    out.save(path, "PNG")
    print("saved", path, out.size)
    return path


if __name__ == "__main__":
    for n, t in THEMES.items():
        make(n, t)
