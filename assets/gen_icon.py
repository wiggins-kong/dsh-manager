# -*- coding: utf-8 -*-
"""生成 DSH 管理器程序图标 (第一版「中枢调度」Control Nexus)。

- 深色圆角方块底座 (深蓝渐变 + 细边框)
- 中央唯一一颗绿色发光核心节点 (#22C55E, 品牌 "run green")
- 三条调度线连到 3 颗浅灰卫星节点 (= 调度/管理多个 DSH 实例)
- boldness 集中: 全图唯一饱和绿色只在核心, 其余克制
产物: assets/app.ico (多尺寸) + assets/app-icon-256.png
"""
from PIL import Image, ImageDraw
import math

BASE = (15, 26, 46)
CARD_TOP = (27, 35, 54)
BORDER = (51, 65, 85)
GREEN = (34, 197, 94)
SATELLITE = (158, 178, 197)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_icon(size: int) -> Image.Image:
    C = size
    img = Image.new("RGBA", (C, C), (0, 0, 0, 0))

    pad = int(C * 0.11)
    radius = int(C * 0.22)
    inner = (pad, pad, C - pad - 1, C - pad - 1)

    bg = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    db = ImageDraw.Draw(bg)
    for y in range(C):
        db.line([(0, y), (C, y)], fill=lerp(CARD_TOP, BASE, y / C))
    mask = Image.new("L", (C, C), 0)
    ImageDraw.Draw(mask).rounded_rectangle(inner, radius=radius, fill=255)
    img.paste(bg, (0, 0), mask)
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle(inner, radius=radius, outline=BORDER,
                         width=max(2, int(C * 0.006)))

    # 中央核心节点: 唯一绿色 + 光晕
    cx, cy = C // 2, C // 2
    core_r = int(C * 0.115)
    for r, a in ((core_r * 1.7, 26), (core_r * 1.4, 40), (core_r * 1.15, 60)):
        rr = int(r)
        dr.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=GREEN + (a,))
    dr.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r],
               fill=GREEN)
    hr = int(core_r * 0.35)
    dr.ellipse([cx - core_r + core_r // 5 - hr, cy - core_r + core_r // 5 - hr,
                cx - core_r + core_r // 5 + hr, cy - core_r + core_r // 5 + hr],
               fill=(255, 255, 255, 120))

    # 三条调度线 + 3 颗卫星 (120° 等距)
    dist = int(C * 0.30)
    sat_r = int(C * 0.05)
    for ang in (-90, 30, 150):
        a = math.radians(ang)
        sx, sy = cx + int(dist * math.cos(a)), cy + int(dist * math.sin(a))
        dr.line([(cx, cy), (sx, sy)], fill=GREEN + (140,),
                width=max(3, int(C * 0.012)))
        dr.ellipse([sx - sat_r, sy - sat_r, sx + sat_r, sy + sat_r],
                   fill=SATELLITE)

    return img


def main():
    src = make_icon(512)
    src.resize((256, 256), Image.LANCZOS).save("assets/app-icon-256.png")
    src.save("assets/app.ico", sizes=[
        (256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("完成(第一版中枢调度): assets/app.ico + app-icon-256.png")


if __name__ == "__main__":
    main()