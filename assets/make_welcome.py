"""
Генератор приветственного баннера для бота (assets/welcome.png).
Запуск: python assets/make_welcome.py
Тема — «пастила»: пастельный фон + кубики пастилы с сахарной обсыпкой.
Эмодзи в картинку не кладём (PIL рисует их ч/б) — они идут в подпись Telegram.
"""
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1280, 720
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "welcome.png")
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
random.seed(7)

# палитра «пастила»
BG_TOP = (255, 214, 228)     # нежно-розовый
BG_BOT = (255, 247, 237)     # крем
P_BASE = (244, 166, 183)     # тело пастилы
P_TOP = (251, 214, 223)      # «сахарная» грань
P_EDGE = (231, 146, 167)
P_LAYER = (235, 138, 165)
SUGAR = (255, 255, 255)
C_TITLE = (194, 37, 92)      # малиновый
C_SUB = (123, 44, 75)        # сливовый
C_HINT = (176, 92, 122)


def bold(size):
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), size)


def reg(size):
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), size)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --- фон: мягкий пастельный градиент ---
base = Image.new("RGBA", (W, H))
d = ImageDraw.Draw(base)
for y in range(H):
    d.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, y / H) + (255,))

# редкая «сахарная пудра» по всему фону
dust = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dd = ImageDraw.Draw(dust)
for _ in range(90):
    x, y = random.randint(0, W), random.randint(0, H)
    r = random.choice([2, 2, 3])
    dd.ellipse([x, y, x + r, y + r], fill=(255, 255, 255, random.randint(60, 130)))
base = Image.alpha_composite(base, dust)
d = ImageDraw.Draw(base)


def pastila(cx, cy, s, check=False, layers=True):
    """Кубик пастилы с центром (cx,cy) и полуразмером s."""
    x0, y0, x1, y1 = cx - s, cy - s, cx + s, cy + s
    rad = int(s * 0.5)
    # мягкая тень
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0 + 6, y0 + 14, x1 + 6, y1 + 14], radius=rad, fill=(150, 70, 95, 90)
    )
    return sh, (x0, y0, x1, y1, rad, s, check, layers)


def draw_pastila(draw, geo):
    x0, y0, x1, y1, rad, s, check, layers = geo
    draw.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=P_BASE,
                           outline=P_EDGE, width=3)
    # слои фруктовой пастилы
    if layers:
        for i in range(1, 3):
            ly = y0 + s * (0.8 + i * 0.45)
            draw.line([(x0 + 12, ly), (x1 - 12, ly)], fill=P_LAYER, width=3)
    # «сахарная» верхняя грань
    draw.rounded_rectangle([x0 + s * 0.16, y0 + s * 0.16, x1 - s * 0.16, y0 + s * 0.72],
                           radius=int(rad * 0.7), fill=P_TOP)
    # сахарная обсыпка
    for _ in range(int(s / 4)):
        px = random.randint(int(x0 + 10), int(x1 - 10))
        py = random.randint(int(y0 + 8), int(y0 + s * 0.66))
        draw.ellipse([px, py, px + 3, py + 3], fill=(255, 255, 255, 235))
    # галочка — задача-пастилка «сделана»
    if check:
        cxx, cyy = (x0 + x1) / 2, (y0 + y1) / 2
        draw.line([(cxx - s * 0.32, cyy + s * 0.02),
                   (cxx - s * 0.06, cyy + s * 0.3),
                   (cxx + s * 0.36, cyy - s * 0.26)],
                  fill=C_TITLE, width=max(6, int(s * 0.12)), joint="curve")


# --- кластер пастилок справа ---
pieces = [
    pastila(858, 330, 78),
    pastila(1098, 322, 70),
    pastila(978, 392, 122, check=True),
    pastila(1012, 168, 44, layers=False),
    pastila(812, 520, 40, layers=False),
]
for sh, _ in pieces:                       # сначала все тени
    base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(10)))
d = ImageDraw.Draw(base)
for _, geo in pieces:                       # затем сами кубики
    draw_pastila(d, geo)

# --- логотип-пастилка слева ---
sh, geo = pastila(126, 152, 46, check=True, layers=False)
base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(8)))
d = ImageDraw.Draw(base)
draw_pastila(d, geo)

# --- текст (строго слева, до x≈720) ---
d.text((196, 152), "Pastila OS", font=bold(72), fill=C_TITLE + (255,), anchor="lm")
d.text((84, 314), "Бот для задач команды", font=bold(46), fill=C_SUB + (255,), anchor="lm")
d.text((84, 384), "Дела по полочкам — как слои пастилы",
       font=reg(28), fill=C_HINT + (255,), anchor="lm")

# чипы команд
cx = 84
for label in ["/new", "/list", "голос"]:
    f = bold(28)
    tw = d.textbbox((0, 0), label, font=f)[2]
    w = tw + 48
    d.rounded_rectangle([cx, 470, cx + w, 526], radius=28, fill=(255, 255, 255, 240))
    d.text((cx + w / 2, 498), label, font=f, fill=C_TITLE + (255,), anchor="mm")
    cx += w + 18

base.convert("RGB").save(OUT, "PNG")
print("saved", OUT, base.size)
