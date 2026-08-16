"""
Generates the 5 "North Coast launches" Instagram carousel slides (1080x1080 PNG).

Brand colors: navy #104480 background, gold #c69d7a for titles / final slide.
Arabic text is shaped with arabic_reshaper + python-bidi so letters connect and
render right-to-left correctly (plain PIL draws unshaped, disconnected Arabic).

Font: Noto Sans Arabic (variable, wght axis). Tajawal was tried first but its
Presentation-Forms-B glyph set is incomplete (missing forms for some letters,
e.g. meem/kaf finals), which showed up as tofu boxes once reshaped -- verified
via fontTools cmap inspection. Noto Sans Arabic has full coverage (141/144 in
that Unicode block) plus the Latin/digits we need for mixed EN/AR lines, so one
font covers everything without per-run font switching.

Usage:
    python3 scripts/generate_sahel_carousel.py
Output:
    scripts/output/sahel_carousel/slide_1.png ... slide_5.png
"""
import os
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "assets", "fonts", "noto_sans_arabic", "NotoSansArabic-Variable.ttf")
OUT_DIR = os.path.join(BASE_DIR, "output", "sahel_carousel")

# Named weights mapped to the font's "wght" variable axis value.
WEIGHTS = {
    "Regular": 400,
    "Medium": 500,
    "Bold": 700,
    "ExtraBold": 800,
}

SIZE = 1080
NAVY = "#104480"
GOLD = "#c69d7a"
WHITE = "#ffffff"
MUTED_WHITE = "#c9d6e8"   # navy-bg secondary text
MUTED_NAVY = "#5a6a85"    # gold-bg secondary text (on slide 5 badge etc.)

EASTERN_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def ar(text):
    """Reshape Arabic text (connect letters) and reorder for correct RTL/mixed display."""
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def font(weight, size):
    f = ImageFont.truetype(FONT_PATH, size)
    f.set_variation_by_axes([WEIGHTS[weight]])
    return f


def fit_font(draw, text, weight, start_size, max_width, min_size=28):
    """Shrink font size until the (already-shaped) text fits within max_width."""
    size = start_size
    while size > min_size:
        f = font(weight, size)
        bbox = draw.textbbox((0, 0), text, font=f)
        if (bbox[2] - bbox[0]) <= max_width:
            return f
        size -= 2
    return font(weight, min_size)


def draw_centered_line(draw, text_raw, y, weight, size, fill, max_width=920):
    shaped = ar(text_raw)
    f = fit_font(draw, shaped, weight, size, max_width)
    bbox = draw.textbbox((0, 0), shaped, font=f)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (SIZE - w) / 2 - bbox[0]
    draw.text((x, y), shaped, font=f, fill=fill)
    return y + h


def draw_centered_block(draw, lines, y, weight, size, fill, line_gap=14, max_width=880):
    """Draw several already-separate lines (from the brief's own line breaks), centered."""
    for line in lines:
        y = draw_centered_line(draw, line, y, weight, size, fill, max_width=max_width) + line_gap
    return y


def draw_corner_index(draw, n, on_gold_bg=False):
    label = ar(str(n).translate(EASTERN_DIGITS) + " / " + "٠٥")
    f = font("Medium", 26)
    color = MUTED_NAVY if on_gold_bg else MUTED_WHITE
    bbox = draw.textbbox((0, 0), label, font=f)
    w = bbox[2] - bbox[0]
    draw.text((SIZE - 60 - w, SIZE - 70), label, font=f, fill=color)


def draw_brand_footer(draw, on_gold_bg=False, prominent=False):
    color = NAVY if on_gold_bg else MUTED_WHITE
    size = 34 if prominent else 22
    weight = "Bold" if prominent else "Medium"
    f = font(weight, size)
    text = "meleveneg.com"
    bbox = draw.textbbox((0, 0), text, font=f)
    w = bbox[2] - bbox[0]
    y = SIZE - 210 if prominent else 60
    draw.text(((SIZE - w) / 2, y), text, font=f, fill=color)


def draw_dev_badge(draw, name):
    f = font("Bold", 26)
    text = name.upper()
    bbox = draw.textbbox((0, 0), text, font=f)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    pad_x, pad_y = 26, 14
    x0 = (SIZE - w) / 2 - pad_x
    y0 = 170
    x1 = (SIZE + w) / 2 + pad_x
    y1 = y0 + h + pad_y * 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) / 2, outline=GOLD, width=2)
    draw.text(((SIZE - w) / 2, y0 + pad_y - bbox[1] / 2 - 2), text, font=f, fill=GOLD)
    return y1


def slide_cover(index):
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    y = draw_centered_line(d, "إطلاقات الساحل الشمالي", 400, "ExtraBold", 86, GOLD, max_width=940)
    draw_centered_line(d, "اللي الكل بيتكلم عنها.", y + 26, "Medium", 44, WHITE, max_width=800)
    draw_corner_index(d, index)
    return img


def slide_project(index, developer, title, body_lines):
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    y_after_badge = draw_dev_badge(d, developer)
    y = draw_centered_line(d, title, y_after_badge + 60, "ExtraBold", 66, GOLD, max_width=940)
    draw_centered_block(d, body_lines, y + 50, "Medium", 40, WHITE, line_gap=16)
    draw_corner_index(d, index)
    return img


def slide_closing(index, headline_lines, url):
    img = Image.new("RGB", (SIZE, SIZE), GOLD)
    d = ImageDraw.Draw(img)
    draw_centered_block(d, headline_lines, 400, "ExtraBold", 56, NAVY, line_gap=20, max_width=880)
    draw_brand_footer(d, on_gold_bg=True, prominent=True)
    draw_corner_index(d, index, on_gold_bg=True)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    slides = [
        slide_cover(1),
        slide_project(
            2, "Palm Hills", "Hacienda Ras El Hekma",
            ["من 11 مليون جنيه", "مقدم 5% · تقسيط حتى 10 سنين", "تسليم ~2030"],
        ),
        slide_project(
            3, "SODIC", "Ogami (فيلات Nobu)",
            ["من 17 مليون جنيه", "مقدم 5% · تقسيط 7 سنين"],
        ),
        slide_project(
            4, "Modon", "Nammos Ras El Hekma",
            ["من 12 مليون جنيه", "مقدم 10% · تقسيط 10 سنين", "تسليم 2030"],
        ),
        slide_closing(
            5,
            ["3 مطورين، 3 خطط تقسيط مختلفة.", "محتاج تعرف تختار أنهي واحدة تناسبك؟"],
            "meleveneg.com",
        ),
    ]

    for i, img in enumerate(slides, start=1):
        path = os.path.join(OUT_DIR, f"slide_{i}.png")
        img.save(path, "PNG")
        print(f"saved {path}")


if __name__ == "__main__":
    main()
