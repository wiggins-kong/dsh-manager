"""将生成的方形 logo 裁圆角并导出 ico。

用法:
    python assets/make_rounded.py assets/whale/A1.png [更多图片...]

输出: 同目录 <名字>-rounded.png (含 alpha 圆角) 及 <名字>.ico (256/64/32)
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

RADIUS_RATIO = 0.20  # 圆角半径占边长比例


def rounded(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    size = img.size
    r = int(min(size) * RADIUS_RATIO)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=r, fill=255)
    img.putalpha(mask)
    return img


def main(paths: list[str]) -> None:
    for p in paths:
        src = Path(p)
        img = Image.open(src)
        out = rounded(img)
        rounded_path = src.with_name(f"{src.stem}-rounded.png")
        out.save(rounded_path)
        out.resize((256, 256), Image.LANCZOS).save(
            src.with_suffix(".ico"),
            sizes=[(256, 256), (64, 64), (32, 32)],
        )
        print(f"{src} -> {rounded_path} + {src.with_suffix('.ico')}")


if __name__ == "__main__":
    main(sys.argv[1:])
