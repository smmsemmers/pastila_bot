"""
Велком-герой Pastila OS (перезаписывает assets/welcome.png).
Запуск: python assets/make_welcome_hero.py
Показывает три бота как команду с иерархией: центр и крупнее — ядро Pastila Code,
по бокам помощники: TaskBot (задачи) и GPT (поиск). Стиль «пастила».
"""
import os
import math
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "welcome.png")
SS = 2
OW, OH = 1280, 720
W, H = OW * SS, OH * SS

_BOLD = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_REG = ["/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _f(c, s):
    for p in c:
        if os.path.exists(p):
            return ImageFont.truetype(p, s)
    return ImageFont.load_default()


def bold(s):
    return _f(_BOLD, s)


def reg(s):
    return _f(_REG, s)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


BG_TOP = (255, 214, 228)
BG_BOT = (255, 247, 237)
C_TITLE = (194, 37, 92)
C_SUB = (123, 44, 75)

CUBES = {  # x-доля центра, размер-доля, тема
    "taskbot": dict(base=(244, 166, 183), top=(251, 214, 223), edge=(231, 146, 167),
                    ink=(194, 37, 92), sym="check", tag="ЗАДАЧИ", name="TaskBot"),
    "bridge": dict(base=(166, 224, 196), top=(214, 244, 226), edge=(140, 205, 172),
                   ink=(30, 130, 96), sym="spark", tag="ЯДРО", name="Pastila Code"),
    "gpt": dict(base=(176, 196, 244), top=(214, 226, 251), edge=(146, 170, 231),
                ink=(54, 78, 168), sym="lens", tag="ПОИСК", name="GPT Remote"),
}


def symbol(d, cx, cy, s, ink, sym):
    if sym == "check":
        d.line([(cx - s * 0.34, cy + s * 0.04), (cx - s * 0.06, cy + s * 0.32),
                (cx + s * 0.40, cy - s * 0.28)], fill=ink,
               width=max(8 * SS, int(s * 0.16)), joint="curve")
    elif sym == "lens":
        r = s * 0.34
        lcx, lcy = cx - s * 0.10, cy - s * 0.12
        d.ellipse([lcx - r, lcy - r, lcx + r, lcy + r], outline=ink,
                  width=max(6 * SS, int(s * 0.12)))
        d.line([(lcx + r * 0.72, lcy + r * 0.72), (cx + s * 0.44, cy + s * 0.44)],
               fill=ink, width=max(8 * SS, int(s * 0.15)))
    elif sym == "spark":
        def star(ccx, ccy, R, r, rot=90):
            return [(ccx + (R if i % 2 == 0 else r) * math.cos(math.radians(rot + i * 45)),
                     ccy - (R if i % 2 == 0 else r) * math.sin(math.radians(rot + i * 45)))
                    for i in range(8)]
        d.polygon(star(cx, cy, s * 0.54, s * 0.17), fill=ink)
        d.polygon(star(cx + s * 0.44, cy - s * 0.42, s * 0.17, s * 0.05), fill=ink)


def cube(base_img, cx, cy, s, t, big=False):
    W_, H_ = base_img.size
    x0, y0, x1, y1 = cx - s, cy - s, cx + s, cy + s
    rad = int(s * 0.42)
    sh = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0 + 8 * SS, y0 + 16 * SS, x1 + 8 * SS, y1 + 16 * SS], radius=rad,
        fill=(150, 70, 95, 90))
    base_img = Image.alpha_composite(base_img, sh.filter(ImageFilter.GaussianBlur(12 * SS)))
    d = ImageDraw.Draw(base_img)
    d.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=t["base"],
                        outline=t["edge"], width=4 * SS)
    d.rounded_rectangle([x0 + s * 0.16, y0 + s * 0.16, x1 - s * 0.16, y0 + s * 0.66],
                        radius=int(rad * 0.7), fill=t["top"])
    for _ in range(int(s / 6)):
        px = random.randint(int(x0 + 14 * SS), int(x1 - 14 * SS))
        py = random.randint(int(y0 + 12 * SS), int(y0 + s * 0.6))
        d.ellipse([px, py, px + 3 * SS, py + 3 * SS], fill=(255, 255, 255, 235))
    symbol(d, cx, cy, s, t["ink"], t["sym"])
    # тег роли
    tagf = bold((26 if big else 22) * SS)
    tb = d.textbbox((0, 0), t["tag"], font=tagf)
    pw = (tb[2] - tb[0]) + 28 * SS
    ph = (44 if big else 38) * SS
    ty = y1 + (30 if big else 24) * SS
    d.rounded_rectangle([cx - pw / 2, ty, cx + pw / 2, ty + ph], radius=ph // 2,
                        fill=t["ink"] + (255,))
    d.text((cx, ty + ph / 2), t["tag"], font=tagf, fill=(255, 255, 255, 255), anchor="mm")
    d.text((cx, ty + ph + (34 if big else 28) * SS), t["name"],
           font=bold((34 if big else 28) * SS), fill=C_SUB + (255,), anchor="mm")
    return base_img


random.seed(7)
img = Image.new("RGBA", (W, H))
d = ImageDraw.Draw(img)
for y in range(H):
    d.line([(0, y), (W, y)], fill=lerp(BG_TOP, BG_BOT, y / H) + (255,))
dust = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dd = ImageDraw.Draw(dust)
for _ in range(160):
    x, y = random.randint(0, W), random.randint(0, H)
    r = random.choice([2, 3, 3, 4]) * SS
    dd.ellipse([x, y, x + r, y + r], fill=(255, 255, 255, random.randint(45, 110)))
img = Image.alpha_composite(img, dust)
d = ImageDraw.Draw(img)

# заголовок
d.text((W / 2, int(H * 0.14)), "Pastila OS", font=bold(96 * SS),
       fill=C_TITLE + (255,), anchor="mm")
d.text((W / 2, int(H * 0.255)), "Команда ИИ-ботов: ядро + два помощника",
       font=reg(38 * SS), fill=C_SUB + (255,), anchor="mm")

# три кубика: центр — ядро (крупнее)
cy = int(H * 0.56)
img = cube(img, int(W * 0.20), cy, int(H * 0.14), CUBES["taskbot"])
img = cube(img, int(W * 0.80), cy, int(H * 0.14), CUBES["gpt"])
img = cube(img, int(W * 0.50), cy - int(H * 0.02), int(H * 0.175), CUBES["bridge"], big=True)

img.convert("RGB").resize((OW, OH), Image.LANCZOS).save(OUT, "PNG")
print("saved", OUT, (OW, OH))
