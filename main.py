import os
import re
import json
import time
import math
import random
import base64
from io import BytesIO
from threading import Thread

import feedparser
import requests
import schedule
from flask import Flask
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from assets.fonts.font_data import NOTO_KUFI_ARABIC_BOLD_B64, LIBERATION_SANS_BOLD_B64

# --- المفاتيح من بيئة ريبليت ---
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

SEEN_FILE = "seen_stories.json"
TEMP_IMAGE = "temp_image.jpg"

IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1350  # نسبة 4:5 (الأنسب لمنشورات فيد انستغرام، بدل الريلز)

# قالب العلامة التجارية الثابت
BRAND_HANDLE = "RADAR.NEWS"
BRAND_ACCENT_COLOR = (86, 210, 255)  # سماوي نيون، يُستخدم لتمييز الجزء الأهم من العنوان

# الخطوط مضمّنة كنص Base64 داخل الكود (assets/fonts/font_data.py) بدل ملفات ثنائية منفصلة،
# عشان يستحيل تتلف بأي تحويل نصي/نهايات أسطر أثناء نقل الملفات عبر Git
NOTO_KUFI_ARABIC_BOLD_BYTES = base64.b64decode(NOTO_KUFI_ARABIC_BOLD_B64)
LIBERATION_SANS_BOLD_BYTES = base64.b64decode(LIBERATION_SANS_BOLD_B64)


def _resolve_basic_layout_engine():
    """يحدد قيمة محرك التخطيط الأساسي بشكل متوافق مع كل إصدارات Pillow (القديمة والحديثة تختلف بتسميته)."""
    layout = getattr(ImageFont, "Layout", None)
    if layout is not None and hasattr(layout, "BASIC"):
        return layout.BASIC
    return getattr(ImageFont, "LAYOUT_BASIC", None)


_BASIC_LAYOUT_ENGINE = _resolve_basic_layout_engine()


def _load_headline_font(size, use_basic_layout=False):
    """يحمّل الخط العربي المضمّن. عند use_basic_layout=True يحاول فرض محرك التخطيط الأساسي،
    وإذا فشل ذلك لأي سبب (اختلاف إصدار Pillow مثلاً) يعود تلقائياً للتحميل الافتراضي بدل رفع استثناء."""
    if use_basic_layout and _BASIC_LAYOUT_ENGINE is not None:
        try:
            return ImageFont.truetype(BytesIO(NOTO_KUFI_ARABIC_BOLD_BYTES), size, layout_engine=_BASIC_LAYOUT_ENGINE)
        except Exception:
            pass
    return ImageFont.truetype(BytesIO(NOTO_KUFI_ARABIC_BOLD_BYTES), size)


def _load_latin_font(size):
    return ImageFont.truetype(BytesIO(LIBERATION_SANS_BOLD_BYTES), size)

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

RSS_FEEDS = [
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
    },
    {
        "name": "Ars Technica",
        "url": "https://feeds.arstechnica.com/arstechnica/index",
    },
    {
        "name": "Wired",
        "url": "https://www.wired.com/feed/rss",
    },
    {
        "name": "Engadget",
        "url": "https://www.engadget.com/rss.xml",
    },
    {
        "name": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
    },
    {
        "name": "IGN",
        "url": "https://feeds.ign.com/ign/all",
    },
]

# هوية بصرية ثابتة تُضاف لكل صورة غلاف: يجب أن تكون الصورة مرتبطة مباشرة بموضوع الخبر (أجهزة/تقنيات ملموسة)
# لا أشخاص حقيقيين ولا شعارات شركات حقيقية (حماية قانونية للحساب)، بدلاً عنها عناصر وتشبيهات بصرية مصممة
BRAND_VISUAL_STYLE = (
    "signature visual identity: moody, atmospheric dark background (deep navy/black) with vivid glowing "
    "neon-blue and purple accent lighting, rich saturated colors, high contrast, cinematic depth "
    "(never muddy, flat, or desaturated), realistic-yet-stylized tech-product photography look, "
    "consistent with a premium technology news brand"
)


@app.route('/')
def home():
    """هذه الصفحة تبقي السيرفر مستيقظاً"""
    return "Radar News Bot is Alive and Running!"


def run_server():
    app.run(host='0.0.0.0', port=5000)


# --- ذاكرة القصص المنشورة سابقاً (لتفادي تكرار نفس الخبر) ---
def load_seen_stories():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_story(story_id):
    seen = load_seen_stories()
    seen.add(story_id)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False, indent=2)


_ARABIC_RENDER_MODE = "unknown"


def _render_char_bytes(font, ch, **kwargs):
    img = Image.new("L", (140, 100), 0)
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), ch, font=font, fill=255, **kwargs)
    return img.tobytes()


def _detect_arabic_render_mode():
    """يحدد مرة واحدة أي طريقة عرض تنتج فعلاً حروفاً عربية مرئية (لا رمز 'الحرف المفقود' tofu) بهذه البيئة.

    مهم: النص يُمرَّر دائماً خاماً بدون أي تشكيل يدوي مسبق (لا arabic_reshaper ولا bidi) - كلا محرّكي
    Pillow (raqm والمحرك الأساسي BASIC) يقومان بتشكيل الحروف وترتيبها آلياً بأنفسهما. التشكيل اليدوي
    المسبق كان هو سبب ظهور المربعات الفارغة: تمرير نص مُشكَّل مسبقاً لأي من المحركين يجعله يحاول
    "تشكيله" مرة ثانية فينتج رموز غير موجودة بالخط (تحقّقنا من هذا مباشرة بالاختبار).

    الفحص: نقارن رسم حرف عربي حقيقي (خام) برسم رمز من نطاق غير مستخدم إطلاقاً (Private Use Area)
    يُرسم دائماً كرمز 'مفقود' من أي خط عادي؛ لو تطابقا فالطريقة لا تعمل فعلياً، وننتقل للتالية.
    يجرّب بالترتيب: raqm (أفضل جودة، يحتاج مكتبة raqm) -> المحرك الأساسي BASIC (مدمج بكل نسخ Pillow،
    يدعم تشكيل العربي وترتيبه ذاتياً بدون أي مكتبات خارجية) -> لا شيء يعمل (حالة نادرة جداً؛ يُبلَّغ
    بخطأ واضح، ويُعرض بدون الكتابة المرسومة على الصورة كملاذ أخير).
    """
    global _ARABIC_RENDER_MODE
    if _ARABIC_RENDER_MODE != "unknown":
        return _ARABIC_RENDER_MODE

    probe_img = Image.new("L", (140, 100), 0)
    probe_draw = ImageDraw.Draw(probe_img)

    try:
        raqm_font = _load_headline_font(80)
        notdef_bytes = _render_char_bytes(raqm_font, chr(0xE000), direction="rtl", language="ar")
        bbox = probe_draw.textbbox((0, 0), "الف", font=raqm_font, direction="rtl", language="ar")
        if bbox and (bbox[2] - bbox[0]) > 5 and _render_char_bytes(raqm_font, "ا", direction="rtl", language="ar") != notdef_bytes:
            _ARABIC_RENDER_MODE = "raqm"
            print("Arabic rendering: using raqm shaping.")
            return _ARABIC_RENDER_MODE
    except Exception:
        pass

    try:
        basic_font = _load_headline_font(80, use_basic_layout=True)
        notdef_bytes = _render_char_bytes(basic_font, chr(0xE000))
        if _render_char_bytes(basic_font, "ا") != notdef_bytes:
            _ARABIC_RENDER_MODE = "basic"
            print("Arabic rendering: using Pillow's built-in BASIC layout engine (raqm unavailable here).")
            return _ARABIC_RENDER_MODE
    except Exception:
        pass

    _ARABIC_RENDER_MODE = None
    print("ERROR: the embedded Arabic font cannot render visible Arabic glyphs in this environment "
          "(tried both raqm and the BASIC layout engine - both produced the missing-glyph placeholder). "
          "Skipping on-image Arabic text as a last-resort safety net; the full headline is still in the caption text.")
    return _ARABIC_RENDER_MODE


def _arabic_font_healthy():
    return _detect_arabic_render_mode() is not None


def _arabic_textbbox(draw, text, font):
    mode = _detect_arabic_render_mode()
    if mode == "raqm":
        return draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    if mode == "basic":
        basic_font = _load_headline_font(font.size, use_basic_layout=True)
        return draw.textbbox((0, 0), text, font=basic_font)
    return draw.textbbox((0, 0), text, font=font)


def _draw_arabic_text(draw, xy, text, font, fill, anchor=None, align="center", stroke_width=0, stroke_fill=None):
    mode = _detect_arabic_render_mode()
    if mode == "raqm":
        draw.text(xy, text, font=font, fill=fill, direction="rtl", language="ar", anchor=anchor, align=align,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)
    elif mode == "basic":
        basic_font = _load_headline_font(font.size, use_basic_layout=True)
        draw.text(xy, text, font=basic_font, fill=fill, anchor=anchor,
                   align=align, stroke_width=stroke_width, stroke_fill=stroke_fill)
    else:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor, align=align,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)


def _wrap_arabic_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = _arabic_textbbox(draw, candidate, font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _space_width(draw, font):
    with_space = _arabic_textbbox(draw, "و و", font)
    without_space = _arabic_textbbox(draw, "وو", font)
    return max(10, (with_space[2] - with_space[0]) - (without_space[2] - without_space[0]))


def _draw_arabic_line_mixed(draw, center_x, y, line, font, normal_color, highlight_color, highlight_words, stroke_width=0):
    """يرسم سطراً عربياً بترتيب RTL صحيح، مع تلوين الكلمات الموجودة بـ highlight_words بلون مختلف."""
    words = line.split()
    widths = [_arabic_textbbox(draw, w, font)[2] - _arabic_textbbox(draw, w, font)[0] for w in words]
    space_w = _space_width(draw, font)
    total_w = sum(widths) + space_w * max(0, len(words) - 1)
    x_cursor = center_x + total_w // 2
    for word, w in zip(words, widths):
        x_cursor -= w
        color = highlight_color if word.strip(".,،؟!") in highlight_words else normal_color
        _draw_arabic_text(draw, (x_cursor, y), word, font, color, anchor="la",
                           stroke_width=stroke_width, stroke_fill=(0, 0, 0, 255) if stroke_width else None)
        x_cursor -= space_w


def clean_html(text):
    clean = re.sub(r"<[^>]+>", "", text)
    clean = (
        clean.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return clean.strip()


# --- جلب الأخبار من مصادر RSS واختيار قصة جديدة ---
def fetch_stories_from_feed(feed_info):
    print(f"  Fetching: {feed_info['name']} ...")
    try:
        feed = feedparser.parse(feed_info["url"])
        stories = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()

            if not title or not link:
                continue

            stories.append({
                "id": link,
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed_info["name"],
                "published": entry.get("published", ""),
            })

        print(f"    Found {len(stories)} stories.")
        return stories
    except Exception as e:
        print(f"    ERROR fetching {feed_info['name']}: {e}")
        return []


def fetch_all_stories():
    all_stories = []
    for feed_info in RSS_FEEDS:
        all_stories.extend(fetch_stories_from_feed(feed_info))
    return all_stories


def pick_story(all_stories, seen_ids):
    unseen = [s for s in all_stories if s["id"] not in seen_ids]

    if not unseen:
        print("  No new unseen stories, recycling older stories...")
        unseen = all_stories

    if not unseen:
        print("  ERROR: no stories found at all.")
        return None

    return random.choice(unseen)


# --- توليد المحتوى العربي (عنوان الصورة + كابشن) بالاعتماد على خبر حقيقي ---
def _extract_field(full_text, field_name):
    for line in full_text.split('\n'):
        if line.strip().startswith(field_name):
            return line.split(":", 1)[1].strip() if ":" in line else ""
    return ""


def generate_arabic_content(story):
    print("Writing Arabic content based on the story...")
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])
    source = story["source"]

    text_prompt = f"""
    أنت صانع محتوى محترف تدير حساب انستقرام عراقي اسمه "رادار نيوز"، متخصص بأخبار التقنية والذكاء الاصطناعي والألعاب والأحداث التكنولوجية المميزة، تحوّل الأخبار التقنية الحقيقية لمنشورات عربية فضولية شديدة الجذب، بهدف الوصول لصفحة الاكتشاف (الإكسبلور).

    هذا خبر تقني حقيقي من مصدر موثوق:
    العنوان: {title}
    الملخص: {summary}
    المصدر: {source}

    القواعد المهمة:
    1. اعتمد فقط على المعلومات الموجودة فعلاً بالخبر أعلاه، ولا تخترع أي حقيقة غير موجودة فيه.
    2. لا تُعِد صياغة العنوان الرئيسي المعروف للخبر حرفياً. ابحث داخل الملخص عن أهم وأغرب تفصيل تقني فيه (رقم صادم، قدرة جديدة غير متوقعة، تأثير مستقبلي)، واجعله محور المحتوى.
    3. العنوان المصوّر: 5 إلى 9 كلمات، يُكتب بخط كبير أسفل الصورة على 2-3 أسطر. لا تستخدم علامة التعجب "!" إطلاقاً.
    4. التمييز: انسخ حرفياً (كلمة لكلمة، بدون أي تغيير) عبارة قصيرة من 2 إلى 4 كلمات من داخل "العنوان" نفسه بالضبط - أهم جزء فيه (الرقم الصادم أو النتيجة المفاجئة) - لتلوينها بلون مميز.
    5. اختم بسؤال حقيقي يحفّز الناس يكتبون تعليق (مو مجرد "شنو رأيك" عام، خليه سؤال مرتبط تحديداً بتفصيل الخبر).
    6. اكتب بالعربية الفصحى المبسطة والسليمة 100%، بدون كلمات إنجليزية.

    استخدم هذا التنسيق بالضبط في ردك (بدون أي نص إضافي خارج هذا التنسيق):
    الملخص: (كلمتين إلى أربع كلمات، وصف بصري ملموس لأهم عنصر تقني بالخبر، لتوليد صورة عنه)
    العنوان: (5 إلى 9 كلمات، بدون علامة تعجب)
    التمييز: (نسخة حرفية طبق الأصل لـ 2-4 كلمات موجودة داخل العنوان أعلاه بالضبط)
    الخطاف: (أول جملة بالمنشور، سؤال أو جملة صادمة قصيرة)
    الجسم: (3 إلى 4 جمل تكشف التفصيل التقني تدريجياً بأسلوب قصصي شيق)
    السؤال الختامي: (سؤال قصير مرتبط تحديداً بالخبر يحفّز التعليقات)
    الهاشتاقات: (8 إلى 10 هاشتاقات عربية وإنجليزية مرتبطة تحديداً بموضوع الخبر، بدون هاشتاقات عامة مكررة)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": text_prompt}],
            temperature=0.9,
        )
        full_text = response.choices[0].message.content.strip()

        topic_summary = _extract_field(full_text, "الملخص") or title
        headline = _extract_field(full_text, "العنوان") or title
        highlight = _extract_field(full_text, "التمييز") or ""
        hook = _extract_field(full_text, "الخطاف") or headline
        body = _extract_field(full_text, "الجسم") or summary[:300]
        closing_question = _extract_field(full_text, "السؤال الختامي") or "شنو رأيكم؟ 👇"
        hashtags = _extract_field(full_text, "الهاشتاقات") or "#رادار_نيوز #RadarNews"

        # لا نثق بالتمييز إلا إذا كان فعلاً جزءاً حرفياً من العنوان (وإلا نتجاهله بأمان بدون أي خطأ)
        if highlight and highlight not in headline:
            highlight = ""

        # تيمبلت ثابت لصفحة "رادار نيوز" — نفس البنية تتكرر بكل منشور لبناء هوية مميزة للصفحة
        caption = (
            f"{hook}\n\n"
            f"{body}\n\n"
            f"💬 {closing_question}\n"
            f"🔁 شير المنشور لصديق مهووس بالتقنية\n\n"
            f"{hashtags} #رادار_نيوز #RadarNews"
        )

        print(f"Content written: {headline}")
        return topic_summary, headline, highlight, caption
    except Exception as e:
        print(f"ERROR generating content: {e}")
        return None, None, None, None


# --- توليد صورة غلاف عمودية آمنة السياسة، مرتبطة مباشرة بموضوع الخبر ---
def build_image_prompt(story, topic_summary):
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert at writing image-generation prompts for a technology news Instagram page. "
                    "Your prompts are vivid, visual, and always policy-safe, written entirely in English. "
                    "Never include violence, blood, real named people, political figures, real company logos or "
                    "trademarks, or any other copyrighted elements. Focus on scene, atmosphere, and concept. "
                    "Never ask the model to render any text, letters, numbers, signs, or writing in the "
                    "image, in any language or script — describe pure visuals only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write a single image-generation prompt for a vertical Instagram cover about this tech news story. "
                    f"The image must feel CONCRETELY and LITERALLY connected to the story's actual subject matter at a "
                    f"glance (specific relevant hardware, devices, generic tech products, robots, screens, abstract "
                    f"data/network visuals fitting the topic) — avoid vague, unrelated dreamlike surrealism. "
                    f"Translate the concept below into a purely visual English scene description — do not quote or "
                    f"embed the original Arabic text anywhere in the prompt.\n\n"
                    f"Core concept (Arabic, translate to a visual scene only): {topic_summary}\n"
                    f"Title: {title}\n"
                    f"Summary: {summary[:500]}\n\n"
                    f"Requirements:\n"
                    f"- Photorealistic-leaning, high-end tech-product-photography style, or clean sleek 3D render style\n"
                    f"- Vertical composition\n"
                    f"- Fill the frame meaningfully with the concrete subject (do not leave large empty areas)\n"
                    f"- ABSOLUTELY NO text, letters, numbers, symbols, signage, or writing anywhere in the image, "
                    f"in any language — this is critical, image models often render garbled text so avoid it entirely\n"
                    f"- NO real, identifiable people (no named CEOs, no realistic human faces representing real "
                    f"individuals) and NO real company logos or trademarks - use generic, unbranded tech objects "
                    f"and devices instead\n"
                    f"- Masterpiece quality, highly detailed, cinematic lighting\n"
                    f"- Apply this {BRAND_VISUAL_STYLE}\n"
                    f"- Output the prompt only, no explanation"
                ),
            },
        ],
        temperature=0.7,
        max_tokens=220,
    )

    return response.choices[0].message.content.strip()


def _save_generated_image(image_response, headline, highlight):
    data = image_response.data[0]
    if getattr(data, "url", None):
        img_data = requests.get(data.url).content
    else:
        img_data = base64.b64decode(data.b64_json)
    with open(TEMP_IMAGE, "wb") as handler:
        handler.write(img_data)
    apply_brand_template(TEMP_IMAGE, headline, highlight)


def _sanitize_headline(text):
    return text.replace("!", "").replace("！", "").strip()


def _crop_to_ratio(img, target_ratio):
    width, height = img.size
    current_ratio = width / height
    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        x0 = (width - new_width) // 2
        return img.crop((x0, 0, x0 + new_width, height))
    new_height = int(width / target_ratio)
    y0 = (height - new_height) // 2
    return img.crop((0, y0, width, y0 + new_height))


def _draw_radar_icon(draw, center, radius, color):
    cx, cy = center
    line_w = max(2, int(radius * 0.14))
    for r in (radius, radius * 0.62):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=line_w)
    dot_r = radius * 0.16
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=color)
    angle = math.radians(-40)
    ex, ey = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
    draw.line([cx, cy, ex, ey], fill=color, width=line_w)


def apply_brand_template(image_path, headline, highlight):
    """يبني تصميم المنشور: صورة مرتبطة بالخبر بالأعلى + لوحة داكنة بالأسفل فيها عنوان كبير مع تمييز لوني."""
    try:
        img = Image.open(image_path).convert("RGB")
        img = _crop_to_ratio(img, IMAGE_WIDTH / IMAGE_HEIGHT).resize((IMAGE_WIDTH, IMAGE_HEIGHT), Image.LANCZOS)

        # تصحيح تلقائي للسطوع والتباين والتشبع لضمان ألوان حيوية غير باهتة
        img = ImageEnhance.Brightness(img).enhance(1.05)
        img = ImageEnhance.Contrast(img).enhance(1.1)
        img = ImageEnhance.Color(img).enhance(1.15)
        img = img.convert("RGBA")

        width, height = img.size
        measure_draw = ImageDraw.Draw(img)

        # نحسب عدد أسطر العنوان أولاً، لأن ارتفاع اللوحة يعتمد عليها (يمنع أي تداخل مع مقبض العلامة)
        headline_healthy = _arabic_font_healthy()
        headline_text = _sanitize_headline(headline) if headline_healthy else ""
        headline_font = _load_headline_font(max(40, width // 15))
        max_text_width = int(width * 0.88)
        lines = _wrap_arabic_text(measure_draw, headline_text, headline_font, max_text_width) if headline_text else []
        if not headline_healthy:
            print("WARNING: skipping on-image Arabic headline (font unhealthy in this environment); full headline is still in the caption text.")

        icon_r = int(width * 0.032)
        line_height = int(width // 15 * 1.3)
        handle_font_size = max(18, width // 38)
        handle_area = int(handle_font_size * 2.6)
        blend_zone = int(height * 0.05)
        top_margin = icon_r * 2 + 48
        bottom_margin = 36

        # ارتفاع اللوحة يُحسب من كل العناصر الفعلية اللي بتُرسم بداخلها (لا رقم ثابت)، مع سقف وحد أدنى معقولين
        content_height = blend_zone + top_margin + line_height * max(1, len(lines)) + handle_area + bottom_margin
        panel_height = max(int(height * 0.28), min(content_height, height - int(height * 0.04)))
        panel_top = height - panel_height

        # تدرّج ناعم من الصورة إلى اللوحة الداكنة الصلبة أسفلها
        gradient = Image.new("RGBA", (width, panel_height), (10, 10, 14, 255))
        gradient_draw = ImageDraw.Draw(gradient)
        for y in range(blend_zone):
            alpha = int(255 * (y / blend_zone))
            gradient_draw.line([(0, y), (width, y)], fill=(10, 10, 14, alpha))
        img.alpha_composite(gradient, (0, panel_top))

        draw = ImageDraw.Draw(img)

        # أيقونة رادار صغيرة أعلى اللوحة (بدل أي شعار نصي، لتفادي أي مشاكل خط)
        icon_cy = panel_top + blend_zone + icon_r + 14
        _draw_radar_icon(draw, (width // 2, icon_cy), icon_r, BRAND_ACCENT_COLOR + (255,))

        # العنوان الكبير أسفل الصورة، مع تمييز لوني لأهم جزء فيه
        y_cursor = icon_cy + icon_r + 34
        if lines:
            highlight_words = set(_sanitize_headline(highlight).split()) if highlight else set()
            for line in lines:
                _draw_arabic_line_mixed(draw, width // 2, y_cursor, line, headline_font,
                                         (255, 255, 255, 255), BRAND_ACCENT_COLOR + (255,), highlight_words)
                y_cursor += line_height

        # مقبض العلامة التجارية أسفل اللوحة، بمسافة ديناميكية بعد آخر سطر بالعنوان (مع سقف يمنعه يتجاوز حدود الصورة)
        handle_font = _load_latin_font(handle_font_size)
        handle_text = f">> {BRAND_HANDLE}"
        handle_y = min(y_cursor + handle_area // 2 + 12, height - bottom_margin - handle_font_size // 2)
        draw.text((width // 2, handle_y), handle_text, font=handle_font,
                   fill=BRAND_ACCENT_COLOR + (255,), anchor="mm")

        img.convert("RGB").save(image_path, "JPEG", quality=95)
    except Exception as e:
        print(f"WARNING: could not apply brand template to image (continuing without it): {str(e)[:200]}")


def generate_cover_image(story, topic_summary, headline, highlight):
    print("Building a safe, story-relevant image prompt...")
    try:
        image_prompt = build_image_prompt(story, topic_summary)
    except Exception as e:
        print(f"ERROR building image prompt: {e}")
        return False

    print("Generating cover image (dall-e-3)...")
    try:
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=image_prompt,
            size="1024x1792",
            quality="hd",
            n=1,
        )
        _save_generated_image(image_response, headline, highlight)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"WARNING: DALL-E refused this image due to content policy: {error_msg[:200]}")
            return False
        print(f"WARNING: dall-e-3 unavailable ({error_msg[:150]}), trying gpt-image-1...")

    try:
        image_response = client.images.generate(
            model="gpt-image-1",
            prompt=image_prompt,
            size="1024x1536",
            quality="high",
            n=1,
        )
        _save_generated_image(image_response, headline, highlight)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"WARNING: gpt-image-1 refused this image due to content policy: {error_msg[:200]}")
        else:
            print(f"ERROR generating image via gpt-image-1: {error_msg[:200]}")
        return False


def upload_to_temp_server(file_path):
    print("Uploading to temporary host (tmpfiles.org)...")
    try:
        with open(file_path, "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": f}).json()
        return res['data']['url'].replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception as e:
        print(f"ERROR uploading file: {e}")
        return None


def post_image_to_instagram(image_url, caption):
    print("Sending photo to Meta for processing...")
    url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media"

    params = {'image_url': image_url, 'caption': caption, 'access_token': ACCESS_TOKEN}
    res = requests.post(url, data=params).json()

    if 'id' not in res:
        print(f"ERROR creating media container: {res}")
        return False

    creation_id = res['id']
    status_url = f"https://graph.facebook.com/v25.0/{creation_id}?fields=status_code&access_token={ACCESS_TOKEN}"

    is_ready = False
    for _ in range(8):
        time.sleep(3)
        status_res = requests.get(status_url).json()
        if status_res.get('status_code') in ('FINISHED', None):
            is_ready = True
            break

    if is_ready:
        print("Publishing to Explore...")
        publish_url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media_publish"
        publish_res = requests.post(publish_url, data={'creation_id': creation_id, 'access_token': ACCESS_TOKEN}).json()

        if 'id' in publish_res:
            print("SUCCESS: published!")
            return True
    return False


def cleanup_temp_files():
    if os.path.exists(TEMP_IMAGE):
        os.remove(TEMP_IMAGE)


def job():
    """دورة الإنتاج الآلية: جلب خبر تقني حقيقي -> محتوى عربي -> صورة بعنوان مكتوب -> نشر كصورة."""
    print("Scheduled run starting: beginning production cycle...")

    seen_ids = load_seen_stories()

    print("Fetching news from RSS sources...")
    all_stories = fetch_all_stories()
    if not all_stories:
        print("ERROR: no stories found. Cancelling cycle.")
        return

    story = pick_story(all_stories, seen_ids)
    if not story:
        print("ERROR: no valid story to publish. Cancelling cycle.")
        return

    print(f"Selected story: {story['title']} ({story['source']})")

    topic_summary, headline, highlight, caption = generate_arabic_content(story)
    if not (topic_summary and headline and caption):
        print("ERROR: failed to generate Arabic content. Cancelling cycle.")
        return

    if not generate_cover_image(story, topic_summary, headline, highlight):
        print("ERROR: failed to generate image. Cancelling cycle.")
        return

    public_image_url = upload_to_temp_server(TEMP_IMAGE)
    if not public_image_url:
        print("ERROR: failed to upload to temp host. Cancelling cycle.")
        cleanup_temp_files()
        return

    if post_image_to_instagram(public_image_url, caption):
        save_seen_story(story["id"])

    cleanup_temp_files()


# --- نقطة انطلاق النظام الآلي ---
if __name__ == "__main__":
    # تشغيل سيرفر النبض في الخلفية
    server_thread = Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # جدولة النشر (التوقيت بـ UTC - سيرفر ريبليت)
    # 11:00 UTC = 2:00 PM توقيت محلي
    # 17:00 UTC = 8:00 PM توقيت محلي
    schedule.every().day.at("11:00").do(job)
    schedule.every().day.at("17:00").do(job)

    print("Automation running in the background... publishing at the scheduled times.")
    print("Keep-alive server is running; you can now link it with UptimeRobot.")

    # حلقة لانهائية لتبقي السكربت يراقب الوقت
    while True:
        schedule.run_pending()
        time.sleep(60)
