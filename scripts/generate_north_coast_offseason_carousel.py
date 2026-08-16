"""
Generates the 5 "North Coast off-season" Instagram carousel slides (1080x1080 PNG).

Brand colors: navy #104480 background, gold #c69d7a for titles / final slide.
Same font/shaping approach as generate_sahel_carousel.py: Noto Sans Arabic
(variable, wght axis) + arabic_reshaper + python-bidi -- verified full glyph
coverage for the text used here, no tofu boxes.

Usage:
    python3 scripts/generate_north_coast_offseason_carousel.py
Output:
    scripts/output/north_coast_offseason_carousel/slide_1.png ... slide_5.png
"""
import os
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "assets", "fonts", "noto_sans_arabic", "NotoSansArabic-Variable.ttf")
OUT_DIR = os.path.join(BASE_DIR, "output", "north_coast_offseason_carousel")

SIZE = 1080
NAVY = "#104480"
GOLD = "#c69d7a"
WHITE = "#ffffff"
MUTED_WHITE = "#c9d6e8"
MUTED_NAVY = "#5a6a85"

WEIGHTS = {
    "Regular": 400,
    "Medium": 500,
    "Bold": 700,
    "ExtraBold": 800,
}


def ar(text):
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def font(weight, size):
    f = ImageFont.truetype(FONT_PATH, size)
    f.set_variation_by_axes([WEIGHTS[weight]])
    return f


def fit_font(draw, text, weight, start_size, max_width, min_size=28):
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


def draw_centered_block(draw, lines, y, weight, size, fill, line_gap=16, max_width=900):
    for line in lines:
        y = draw_centered_line(draw, line, y, weight, size, fill, max_width=max_width) + line_gap
    return y


def draw_swipe_arrow(draw, on_gold_bg=False):
    """Small right-pointing chevron in the same bottom-right corner the numbered
    index used to sit in -- a "swipe for more" hint. Not drawn on the closing
    slide (nothing further to swipe to)."""
    color = MUTED_NAVY if on_gold_bg else MUTED_WHITE
    size = 22
    x = SIZE - 60 - size
    y = SIZE - 70
    points = [(x, y - size / 2), (x + size, y), (x, y + size / 2)]
    draw.line(points, fill=color, width=5, joint="curve")


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


# Unified vertical start: the first content element on every slide (1-5) begins
# at this same y, so flipping through the carousel doesn't jump between heights.
CONTENT_TOP = 420


def draw_tag_badge(draw, text_raw):
    shaped = ar(text_raw)
    f = font("Bold", 30)
    bbox = draw.textbbox((0, 0), shaped, font=f)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    pad_x, pad_y = 30, 14
    x0 = (SIZE - w) / 2 - pad_x
    y0 = CONTENT_TOP
    x1 = (SIZE + w) / 2 + pad_x
    y1 = y0 + h + pad_y * 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) / 2, outline=GOLD, width=2)
    draw.text(((SIZE - w) / 2 - bbox[0], y0 + pad_y - bbox[1] / 2 - 2), shaped, font=f, fill=GOLD)
    return y1


def slide_cover():
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    y = draw_centered_line(d, "الصيف خلص.", CONTENT_TOP, "ExtraBold", 86, GOLD, max_width=940)
    draw_centered_line(d, "دلوقتي أذكى وقت للشراء في الساحل.", y + 26, "Medium", 42, WHITE, max_width=880)
    draw_swipe_arrow(d)
    return img


def slide_stat(lines):
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    draw_centered_block(d, lines, CONTENT_TOP, "Bold", 50, GOLD, line_gap=22, max_width=920)
    draw_swipe_arrow(d)
    return img


def slide_tagged(tag, lines):
    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    y_after_tag = draw_tag_badge(d, tag)
    draw_centered_block(d, lines, y_after_tag + 70, "Medium", 46, WHITE, line_gap=20, max_width=900)
    draw_swipe_arrow(d)
    return img


def slide_closing(headline_lines, url):
    img = Image.new("RGB", (SIZE, SIZE), GOLD)
    d = ImageDraw.Draw(img)
    draw_centered_block(d, headline_lines, CONTENT_TOP, "ExtraBold", 58, NAVY, line_gap=20, max_width=880)
    draw_brand_footer(d, on_gold_bg=True, prominent=True)
    # No swipe arrow here -- this is the last slide, nothing further to swipe to.
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    slides = [
        slide_cover(),
        slide_stat(
            [
                "الساحل الشمالي لوحده بيمثّل حوالي تلت",
                "مبيعات الكومباوندات في مصر.",
                "أكتر من 70 مشروع على مساحة 35 ألف فدان تقريبًا.",
            ],
        ),
        slide_tagged(
            "رأس الحكمة",
            [
                "مشروع Nammos الجديد اتعلن يوليو 2026.",
                "ومطار رأس الحكمة الدولي هيفتح على",
                "مراحل من آخر السنة.",
            ],
        ),
        slide_tagged(
            "ليه دلوقتي؟",
            [
                "من غير زحمة الموسم، تقدر تعاين براحتك،",
                "وتفاوض من غير ضغط،",
                "وتختار قبل ما الوحدات الكويسة تخلص.",
            ],
        ),
        slide_closing(
            ["جاهز تشوف اللي جديد على الساحل؟"],
            "meleveneg.com",
        ),
    ]

    for i, img in enumerate(slides, start=1):
        path = os.path.join(OUT_DIR, f"slide_{i}.png")
        img.save(path, "PNG")
        print(f"saved {path}")


if __name__ == "__main__":
    main()
