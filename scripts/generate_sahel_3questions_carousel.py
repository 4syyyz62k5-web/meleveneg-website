"""
Generates the 6 "3 questions before you buy on the North Coast" Instagram
carousel slides (1080x1080 PNG).

Same approach as the earlier carousels in this series (generate_sahel_carousel.py,
generate_north_coast_offseason_carousel.py): navy #104480 background, gold
#c69d7a for titles, Noto Sans Arabic (variable, wght axis) + arabic_reshaper +
python-bidi for correctly-shaped Arabic. Slides 2-5 stay flat brand-color, no
photography. Slides 1 and 6 use a real photo of one of our own North Coast
compounds (Compound.cover_image_url from the live DB -- picked for having no
other developer's logo/campaign text baked in, see assets/photos/NOTICE.txt)
center-cropped to the 1080x1080 canvas with a dark navy gradient overlay
underneath the text for legibility.

Every slide's whole text stack (title + body, whatever it has) is vertically
centered in the 1080x1080 canvas, not pinned to a fixed start y -- draw_vertically_centered()
renders the content once on a scratch canvas purely to measure its total
height, then renders it for real starting at (SIZE - total_height) / 2. This
keeps every slide's block visually centered regardless of how many lines it
has, which is what "looks centered like the others" means when line counts
differ across slides -- a fixed start y does NOT produce that (short content
sits high, long content sits low).

Usage:
    python3 scripts/generate_sahel_3questions_carousel.py
Output:
    scripts/output/sahel_3questions_carousel/slide_1.png ... slide_6.png
"""
import os
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "assets", "fonts", "noto_sans_arabic", "NotoSansArabic-Variable.ttf")
PHOTO_DIR = os.path.join(BASE_DIR, "assets", "photos")
OUT_DIR = os.path.join(BASE_DIR, "output", "sahel_3questions_carousel")

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
    """Small right-pointing chevron, bottom-right corner -- a "swipe for more"
    hint. Not drawn on the closing slide (nothing further to swipe to)."""
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


def load_photo_background(filename, overlay_top_alpha=165, overlay_bottom_alpha=245):
    """Opens a photo, center-crops it to a 1080x1080 square, and lays a navy
    gradient (lighter at top, darker toward the bottom where most text sits)
    over it so white/gold text stays legible. Returns an RGB Image."""
    path = os.path.join(PHOTO_DIR, filename)
    photo = Image.open(path).convert("RGB")
    w, h = photo.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    photo = photo.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)

    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    navy_rgb = tuple(int(NAVY.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    for y in range(SIZE):
        alpha = int(overlay_top_alpha + (overlay_bottom_alpha - overlay_top_alpha) * (y / SIZE))
        od.line([(0, y), (SIZE, y)], fill=(*navy_rgb, alpha))

    return Image.alpha_composite(photo.convert("RGBA"), overlay).convert("RGB")


def draw_vertically_centered(real_draw, content_fn):
    """content_fn(draw, y) draws a slide's whole text stack starting at y and
    returns the y just past its last line. Rendered once on a throwaway canvas
    (pure measurement -- draw.textbbox calls inside content_fn don't touch real
    pixels) to get its total height, then rendered for real on `real_draw`
    starting at the y that centers that height in the 1080x1080 canvas."""
    scratch = ImageDraw.Draw(Image.new("RGB", (SIZE, SIZE)))
    total_height = content_fn(scratch, 0)
    start_y = (SIZE - total_height) / 2
    content_fn(real_draw, start_y)


def slide_cover():
    def content(d, y):
        y = draw_centered_line(d, "قبل ما تشتري في الساحل الشمالي", y, "ExtraBold", 64, GOLD, max_width=960)
        y = draw_centered_line(d, "اسأل 3 أسئلة مهمة.", y + 26, "Medium", 44, WHITE, max_width=880) + 40
        y = draw_centered_line(d, "الموقع · المطور · نظام السداد", y, "Medium", 30, MUTED_WHITE, max_width=880)
        return y

    img = load_photo_background("slide1_koun.webp")
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    draw_vertically_centered(d, content)
    draw_swipe_arrow(d)
    return img


def slide_question(question_title, body_lines):
    def content(d, y):
        y = draw_centered_line(d, question_title, y, "ExtraBold", 64, GOLD, max_width=900) + 40
        y = draw_centered_block(d, body_lines, y, "Medium", 40, WHITE, line_gap=18, max_width=920)
        return y

    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    draw_vertically_centered(d, content)
    draw_swipe_arrow(d)
    return img


def slide_synthesis(title, body_lines):
    def content(d, y):
        y = draw_centered_line(d, title, y, "ExtraBold", 60, GOLD, max_width=920) + 40
        y = draw_centered_block(d, body_lines, y, "Medium", 42, WHITE, line_gap=18, max_width=900)
        return y

    img = Image.new("RGB", (SIZE, SIZE), NAVY)
    d = ImageDraw.Draw(img)
    draw_brand_footer(d, prominent=False)
    draw_vertically_centered(d, content)
    draw_swipe_arrow(d)
    return img


def slide_closing(title, subtitle, cta_line):
    def content(d, y):
        y = draw_centered_line(d, title, y, "ExtraBold", 60, GOLD, max_width=900) + 26
        y = draw_centered_line(d, subtitle, y, "Bold", 40, WHITE, max_width=860) + 40
        y = draw_centered_line(d, cta_line, y, "Medium", 32, WHITE, max_width=840)
        return y

    # Photo background (was flat gold) -- gold title + white text over the dark
    # overlay reads better against a photo than the previous navy-on-gold pair,
    # and matches slide 1's photo-slide treatment for visual consistency.
    img = load_photo_background("slide6_crysta.webp")
    d = ImageDraw.Draw(img)
    draw_vertically_centered(d, content)
    draw_brand_footer(d, prominent=True)
    # No swipe arrow here -- this is the last slide, nothing further to swipe to.
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    slides = [
        slide_cover(),
        slide_question(
            "فين؟",
            [
                "الموقع هو كل حاجة.",
                "الـaccess، قرب الخدمات، والمرحلة اللي المشروع فيها،",
                "هتحدد قيمة وحدتك النهارده وبكرة.",
            ],
        ),
        slide_question(
            "مين المطور؟",
            [
                "اسم كبير مش كفاية.",
                "دور على المطور اللي عنده سجل تنفيذ قوي",
                "ومشروعات مكتملة على أرض الواقع.",
            ],
        ),
        slide_question(
            "هتدفع إزاي؟",
            [
                "السعر مجرد رقم... لكن الخطة هي الأهم.",
                "اختار خطة سداد تناسب أهدافك وتحافظ على سيولتك،",
                "مش بس تدور على أقل مقدم.",
            ],
        ),
        slide_synthesis(
            "اختيار العقار الصح",
            [
                "مش مجرد إنك تلاقي وحدة.",
                "هو إنك تعرف ليه الوحدة دي مناسبة ليك.",
            ],
        ),
        slide_closing(
            "مستقبل الساحل",
            "بيبدأ من اختيارك الصح النهارده.",
            "شوف أقوى الفرص المتاحة حاليًا في الساحل مع Meleven.",
        ),
    ]

    for i, img in enumerate(slides, start=1):
        path = os.path.join(OUT_DIR, f"slide_{i}.png")
        img.save(path, "PNG")
        print(f"saved {path}")


if __name__ == "__main__":
    main()
