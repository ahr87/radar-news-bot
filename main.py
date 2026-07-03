import os
import re
import json
import time
import math
import ctypes
import ctypes.util
import random
import base64
import urllib.parse
from io import BytesIO
from threading import Thread

import feedparser
import requests
import schedule
from flask import Flask
from openai import OpenAI


# --- تحميل مكتبة libraqm يدوياً قبل أي استخدام لـ PIL ---
# في بيئة Replit، حزمة libraqm (Nix) تُثبَّت بنجاح لكن مسارها لا يصل تلقائياً لمتغير
# LD_LIBRARY_PATH بالعملية الجارية (فرق بين REPLIT_LD_LIBRARY_PATH ومتغير LD_LIBRARY_PATH
# الفعلي)، فيفشل PIL بالعثور على raqm عبر آليته المعتادة (dlopen حسب اسم المكتبة فقط)،
# ويرجع تلقائياً لمحرك "BASIC" الذي يرسم حروف الكلمة الواحدة بترتيب المصفوفة النصية
# مباشرة بدون تشكيل عربي ولا BiDi -> هذا هو سبب انعكاس الحروف داخل كل كلمة (وليس bug بالكود).
# الحل: نحمّل ملف libraqm.so بأنفسنا عبر ctypes مع RTLD_GLOBAL، فيصبح متوفراً بالذاكرة
# ويجده PIL عند أول استخدام لمحرك RAQM، بدون أي اعتماد على متغيرات البيئة أو ترتيب الإقلاع.
def _preload_native_raqm():
    lib_name = ctypes.util.find_library("raqm")
    if lib_name:
        try:
            ctypes.CDLL(lib_name, mode=ctypes.RTLD_GLOBAL)
            return True
        except OSError:
            pass

    # مهم: لا نستخدم glob على /nix/store مباشرة (فحص المجلد بالكامل قد يتجمّد لثوانٍ طويلة
    # في بيئة Replit لضخامته). بدلاً من ذلك نقرأ المسارات الجاهزة التي وفّرها Nix نفسه
    # بمتغيرات البيئة (فحص وجود ملف واحد محدد المسار = فوري، بعكس مسح مجلد كامل).
    candidate_dirs = []
    for var in ("REPLIT_LD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        value = os.environ.get(var, "")
        candidate_dirs.extend(p for p in value.split(":") if p)

    for directory in candidate_dirs:
        for filename in ("libraqm.so.0", "libraqm.so"):
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                    return True
                except OSError:
                    continue

    print("WARNING: libraqm not found on this system — Arabic text shaping/order may render incorrectly.")
    return False


_RAQM_AVAILABLE = _preload_native_raqm()

from PIL import Image, ImageDraw, ImageFont, ImageEnhance, features

if _RAQM_AVAILABLE and not features.check("raqm"):
    print("WARNING: libraqm was loaded but Pillow still doesn't report raqm support.")

from assets.fonts.font_data import NOTO_KUFI_ARABIC_BOLD_B64, LIBERATION_SANS_BOLD_B64

# --- المفاتيح من بيئة ريبليت ---
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

SEEN_FILE = "seen_stories.json"
TEMP_IMAGE = "temp_image.jpg"
TEMP_IMAGE_2 = "temp_image_2.jpg"

IMAGE_WIDTH = 1080
IMAGE_HEIGHT = 1350  # نسبة 4:5 (الأنسب لمنشورات فيد انستغرام، بدل الريلز)

# قالب العلامة التجارية الثابت
BRAND_HANDLE = "RADAR.NEWS"
BRAND_ACCENT_COLOR = (86, 210, 255)  # سماوي نيون، يُستخدم لتمييز الجزء الأهم من العنوان

# الخطوط مضمّنة كنص Base64 داخل الكود (assets/fonts/font_data.py) بدل ملفات ثنائية منفصلة،
# عشان يستحيل تتلف بأي تحويل نصي/نهايات أسطر أثناء نقل الملفات عبر Git
NOTO_KUFI_ARABIC_BOLD_BYTES = base64.b64decode(NOTO_KUFI_ARABIC_BOLD_B64)
LIBERATION_SANS_BOLD_BYTES = base64.b64decode(LIBERATION_SANS_BOLD_B64)


def _load_headline_font(size):
    return ImageFont.truetype(BytesIO(NOTO_KUFI_ARABIC_BOLD_BYTES), size, layout_engine=ImageFont.Layout.RAQM)


def _load_latin_font(size):
    return ImageFont.truetype(BytesIO(LIBERATION_SANS_BOLD_BYTES), size, layout_engine=ImageFont.Layout.RAQM)

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
    # مصادر إضافية للتسريبات والأخبار الحصرية (محتوى مفاجئ عادة يحقق تفاعل أعلى)
    {
        "name": "MacRumors",
        "url": "https://www.macrumors.com/macrumors.xml",
    },
    {
        "name": "9to5Google",
        "url": "https://9to5google.com/feed/",
    },
    {
        "name": "9to5Mac",
        "url": "https://9to5mac.com/feed/",
    },
    {
        "name": "Tom's Hardware",
        "url": "https://www.tomshardware.com/feeds/all",
    },
    {
        "name": "XDA Developers",
        "url": "https://www.xda-developers.com/feed/",
    },
    {
        "name": "Hacker News",
        "url": "https://hnrss.org/frontpage",
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


# --- رسم النص العربي بشكل متين ومستقل عن بيئة التشغيل ---
# التشخيص المؤكد: بدون مكتبة libraqm فعلياً متوفرة لـ PIL، يرجع محرك الخط لوضع "BASIC"
# الذي يرسم حروف أي كلمة عربية بترتيب المصفوفة النصية مباشرة (بدون تشكيل ولا BiDi) ->
# هذا يعكس ترتيب الحروف داخل كل كلمة (وسابقاً كان يعكس ترتيب الكلمات كذلك حسب البيئة).
# تم تثبيت التبعية النظامية (Nix) libraqm وتحميلها يدوياً عبر ctypes قبل أي استخدام لـ PIL
# (انظر _preload_native_raqm أعلى الملف)، وربط الخطوط بمحرك ImageFont.Layout.RAQM.
# بهذا يقوم raqm بنفسه بتشكيل حروف كل كلمة وترتيبها بصرياً بشكل صحيح تلقائياً.
# نبقي رسم كل كلمة على حدة وترتيب الكلمات يميناً->يساراً بأنفسنا (هذا الجزء كان صحيحاً دوماً)
# لأنه ضروري لتلوين كلمات التمييز بلون مختلف عن باقي العنوان.

def _word_advance(draw, word, font):
    return draw.textlength(word, font=font, direction="rtl", language="ar")


# أسماء الشركات/المنتجات الأجنبية (OpenAI, iPhone...) قد تظهر داخل العنوان بحكم قاعدة المحتوى
# الجديدة. خط "Noto Kufi Arabic" لا يحتوي غالباً على كل حروف اللاتينية بشكل موثوق، فيظهر
# مربعات فارغة (tofu) تماماً كمشكلة التشكيل العربي القديمة. الحل: كشف الكلمات اللاتينية
# واختيار خط لاتيني (Liberation Sans) لها تحديداً، كلمة بكلمة، بدل خط واحد لكل السطر.
def _is_latin_word(word):
    return not any('؀' <= ch <= 'ۿ' or 'ݐ' <= ch <= 'ݿ' for ch in word)


def _font_for_word(word, size):
    return _load_latin_font(size) if _is_latin_word(word) else _load_headline_font(size)


def _wrap_arabic_words(draw, text, size, max_width):
    """يقسّم النص إلى أسطر، كل سطر قائمة كلمات (بترتيبها المنطقي)، بحيث لا يتجاوز عرض السطر max_width.
    كل كلمة تُقاس بالخط المناسب لها (عربي أو لاتيني) حسب محتواها."""
    words = text.split()
    space_w = _word_advance(draw, " ", _load_headline_font(size))
    lines, current, current_w = [], [], 0
    for word in words:
        w = _word_advance(draw, word, _font_for_word(word, size))
        add_w = w + (space_w if current else 0)
        if current and current_w + add_w > max_width:
            lines.append(current)
            current, current_w = [word], w
        else:
            current.append(word)
            current_w += add_w
    if current:
        lines.append(current)
    return lines


def _draw_rtl_line(draw, center_x, y, words, size, normal_color, highlight_color, highlight_words,
                    stroke_width=0, stroke_fill=None):
    """يرسم قائمة كلمات عربية بترتيب يميني صحيح حول مركز أفقي، مع تلوين كلمات التمييز بلون مختلف.
    كل كلمة تُرسم بالخط المناسب لها (عربي أو لاتيني) حسب محتواها، لتفادي مربعات فارغة بأسماء أجنبية."""
    space_w = _word_advance(draw, " ", _load_headline_font(size))
    fonts = [_font_for_word(w, size) for w in words]
    advances = [_word_advance(draw, w, f) for w, f in zip(words, fonts)]
    total_w = sum(advances) + space_w * max(0, len(words) - 1)
    x_cursor = center_x + total_w / 2
    for word, adv, font in zip(words, advances, fonts):
        x_cursor -= adv
        color = highlight_color if word.strip(".,،؟!") in highlight_words else normal_color
        draw.text((x_cursor, y), word, font=font, fill=color, anchor="la",
                   stroke_width=stroke_width, stroke_fill=stroke_fill,
                   direction="rtl", language="ar")
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
    5. اذكر بوضوح اسم الشركة أو المنصة أو المنتج المحدد المذكور بالخبر ضمن "الجسم" — ممنوع تترك القارئ لا يعرف عن أي جهة/خدمة يتحدث الخبر. لا تكتب وصفاً عاماً غامضاً ("عرض خاص"، "منصة جديدة"...) بدون تسمية الجهة صراحة، حتى لو تطلّب هذا ذكر اسمها الأجنبي كما هو.
    6. اختم بسؤال حقيقي يحفّز الناس يكتبون تعليق (مو مجرد "شنو رأيك" عام، خليه سؤال مرتبط تحديداً بتفصيل الخبر).
    7. اكتب بالعربية الفصحى المبسطة والسليمة 100%، ما عدا أسماء الشركات/المنتجات الأجنبية نفسها (تُكتب كما هي بدون ترجمة).

    استخدم هذا التنسيق بالضبط في ردك (بدون أي نص إضافي خارج هذا التنسيق):
    الملخص: (كلمتين إلى أربع كلمات، وصف بصري ملموس لأهم عنصر تقني بالخبر، لتوليد صورة عنه)
    العنوان: (5 إلى 9 كلمات، بدون علامة تعجب)
    التمييز: (نسخة حرفية طبق الأصل لـ 2-4 كلمات موجودة داخل العنوان أعلاه بالضبط)
    الخطاف: (أول جملة بالمنشور، سؤال أو جملة صادمة قصيرة)
    الجسم: (3 إلى 4 جمل تكشف التفصيل التقني تدريجياً بأسلوب قصصي شيق، تذكر اسم الشركة/المنصة صراحة)
    السؤال الختامي: (سؤال قصير مرتبط تحديداً بالخبر يحفّز التعليقات)
    الهاشتاقات: (5 هاشتاقات فقط، عربية وإنجليزية، مرتبطة تحديداً وحصراً بموضوع هذا الخبر بالذات - ممنوع أي هاشتاق عام يصلح لأي خبر آخر)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
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
        return topic_summary, headline, highlight, caption, hook, closing_question
    except Exception as e:
        print(f"ERROR generating content: {e}")
        return None, None, None, None, None, None


# --- توليد صورة غلاف عمودية آمنة السياسة، مرتبطة مباشرة بموضوع الخبر ---
def build_image_prompt(story, topic_summary):
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])

    response = client.chat.completions.create(
        model="gpt-4o-mini",
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


# نطاقات الحروف المسموحة برسمها على الصورة: عربي (بأشكاله وتشكيله) + لاتيني/أرقام/علامات ترقيم أساسية.
# أي رمز خارج هذا النطاق (إيموجي، رموز زخرفية، رموز كيكاب للأرقام...) لا تحتويه خطوطنا المضمّنة
# (Noto Kufi Arabic و Liberation Sans) فيظهر كمربع فارغ (tofu) — بدل محاولة استثناء كل رمز
# محتمل يدوياً (بلا نهاية وقابل للفشل مستقبلاً)، نستخدم قائمة سماح (whitelist) تضمن عدم تكرار
# هذا الخلل بغض النظر عن أي رمز غير متوقع يولّده الذكاء الاصطناعي مستقبلاً.
_ALLOWED_CHAR_RANGES = (
    (0x0020, 0x007E),  # ASCII: حروف لاتينية، أرقام، علامات ترقيم أساسية، مسافة
    (0x0600, 0x06FF),  # عربي أساسي (بما فيه التشكيل)
    (0x0750, 0x077F),  # ملحق عربي
    (0x08A0, 0x08FF),  # عربي ممتد-أ
    (0xFB50, 0xFDFF),  # أشكال عرض عربية-أ
    (0xFE70, 0xFEFF),  # أشكال عرض عربية-ب
)


# حروف مسموحة يونيكودياً (ضمن _ALLOWED_CHAR_RANGES) لكن خط NotoKufiArabic-Bold تحديداً
# لا يملك لها Glyph سليم (تظهر tofu حتى بمعزل عن أي مشكلة تشكيل) — تأكدنا من هذا سابقاً
# بالاختبار المباشر، فنحذفها صراحة بغض النظر عن التزام الذكاء الاصطناعي بتعليمات البرومت.
_BROKEN_GLYPH_CHARS = "!！"


def _sanitize_headline(text):
    cleaned = "".join(
        ch for ch in text
        if ch not in _BROKEN_GLYPH_CHARS
        and (ch.isspace() or any(lo <= ord(ch) <= hi for lo, hi in _ALLOWED_CHAR_RANGES))
    )
    return re.sub(r"\s+", " ", cleaned).strip()


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

        # نحسب أسطر العنوان أولاً (كل سطر قائمة كلمات)، لأن ارتفاع اللوحة يعتمد عليها (يمنع أي تداخل)
        headline_text = _sanitize_headline(headline)
        headline_size = max(40, width // 15)
        max_text_width = int(width * 0.88)
        lines = _wrap_arabic_words(measure_draw, headline_text, headline_size, max_text_width) if headline_text else []

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

        # العنوان الكبير أسفل الصورة، مع تمييز لوني لأهم جزء فيه (كل كلمة تُرسم منفردة بترتيب يميني)
        y_cursor = icon_cy + icon_r + 34
        highlight_words = set(_sanitize_headline(highlight).split()) if highlight else set()
        stroke_w = max(2, width // 260)
        for line_words in lines:
            _draw_rtl_line(draw, width // 2, y_cursor, line_words, headline_size,
                            (255, 255, 255, 255), BRAND_ACCENT_COLOR + (255,), highlight_words,
                            stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
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


def build_detail_slide(hook, closing_question):
    """يبني شريحة كاروسيل ثانية (بطاقة نصية بهوية الحساب): أهم جملة من الخبر + سؤال يحفّز التعليقات.
    ترفع وقت المكوث بالمنشور (Dwell Time) وتشجّع التعليقات — من أهم عوامل الوصول لصفحة الاكتشاف.
    لا تحتاج أي توليد صورة بالذكاء الاصطناعي (رسم مباشر)، فلا تضيف أي تكلفة إضافية."""
    try:
        img = Image.new("RGBA", (IMAGE_WIDTH, IMAGE_HEIGHT), (8, 10, 20, 255))
        draw = ImageDraw.Draw(img)

        for y in range(IMAGE_HEIGHT):
            t = y / IMAGE_HEIGHT
            fill = (int(8 + 12 * t), int(10 + 4 * t), int(20 + 20 * t), 255)
            draw.line([(0, y), (IMAGE_WIDTH, y)], fill=fill)

        icon_r = int(IMAGE_WIDTH * 0.045)
        icon_cy = int(IMAGE_HEIGHT * 0.14)
        _draw_radar_icon(draw, (IMAGE_WIDTH // 2, icon_cy), icon_r, BRAND_ACCENT_COLOR + (255,))

        max_w = int(IMAGE_WIDTH * 0.86)
        stroke_w = max(2, IMAGE_WIDTH // 260)

        hook_text = _sanitize_headline(hook)
        hook_size = max(48, IMAGE_WIDTH // 12)
        hook_lines = _wrap_arabic_words(draw, hook_text, hook_size, max_w) if hook_text else []
        line_height = int(IMAGE_WIDTH // 12 * 1.35)
        y_cursor = (IMAGE_HEIGHT - line_height * len(hook_lines)) // 2
        for line_words in hook_lines:
            _draw_rtl_line(draw, IMAGE_WIDTH // 2, y_cursor, line_words, hook_size,
                            (255, 255, 255, 255), BRAND_ACCENT_COLOR + (255,), set(),
                            stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
            y_cursor += line_height

        question_text = _sanitize_headline(closing_question)
        question_size = max(32, IMAGE_WIDTH // 22)
        q_lines = _wrap_arabic_words(draw, question_text, question_size, max_w) if question_text else []
        q_line_height = int(IMAGE_WIDTH // 22 * 1.3)
        q_y = IMAGE_HEIGHT - int(IMAGE_HEIGHT * 0.15) - q_line_height * len(q_lines)
        for line_words in q_lines:
            _draw_rtl_line(draw, IMAGE_WIDTH // 2, q_y, line_words, question_size,
                            BRAND_ACCENT_COLOR + (255,), BRAND_ACCENT_COLOR + (255,), set(),
                            stroke_width=max(1, IMAGE_WIDTH // 400), stroke_fill=(0, 0, 0, 255))
            q_y += q_line_height

        handle_font_size = max(18, IMAGE_WIDTH // 38)
        handle_font = _load_latin_font(handle_font_size)
        draw.text((IMAGE_WIDTH // 2, IMAGE_HEIGHT - 48), f">> {BRAND_HANDLE}", font=handle_font,
                   fill=BRAND_ACCENT_COLOR + (255,), anchor="mm")

        img.convert("RGB").save(TEMP_IMAGE_2, "JPEG", quality=95)
        return True
    except Exception as e:
        print(f"WARNING: could not build detail slide (continuing with single-image post): {str(e)[:200]}")
        return False


def _generate_image_pollinations(prompt):
    """توليد صورة مجاني بالكامل (بدون مفتاح API) عبر Pollinations.ai، كخيار أول لتقليل تكلفة OpenAI."""
    print("Generating cover image (pollinations.ai, free)...")
    try:
        encoded_prompt = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        params = {"width": 1024, "height": 1536, "nologo": "true"}
        resp = requests.get(url, params=params, timeout=90)
        resp.raise_for_status()
        if not resp.content or len(resp.content) < 1000:
            raise ValueError("empty or too-small response")
        return resp.content
    except Exception as e:
        print(f"WARNING: pollinations.ai failed ({str(e)[:150]}), falling back to OpenAI image models...")
        return None


def generate_cover_image(story, topic_summary, headline, highlight):
    print("Building a safe, story-relevant image prompt...")
    try:
        image_prompt = build_image_prompt(story, topic_summary)
    except Exception as e:
        print(f"ERROR building image prompt: {e}")
        return False

    img_data = _generate_image_pollinations(image_prompt)
    if img_data:
        with open(TEMP_IMAGE, "wb") as handler:
            handler.write(img_data)
        apply_brand_template(TEMP_IMAGE, headline, highlight)
        return True

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


def _create_media_container(params):
    url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media"
    res = requests.post(url, data=params).json()
    if 'id' not in res:
        print(f"ERROR creating media container: {res}")
        return None
    return res['id']


def _wait_until_ready(creation_id):
    status_url = f"https://graph.facebook.com/v25.0/{creation_id}?fields=status_code&access_token={ACCESS_TOKEN}"
    status_res = {}
    for _ in range(8):
        time.sleep(3)
        status_res = requests.get(status_url).json()
        if status_res.get('status_code') in ('FINISHED', None):
            return True
    print(f"ERROR: media container {creation_id} never became ready: {status_res}")
    return False


def _publish_container(creation_id):
    publish_url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media_publish"
    publish_res = requests.post(publish_url, data={'creation_id': creation_id, 'access_token': ACCESS_TOKEN}).json()
    if 'id' not in publish_res:
        print(f"ERROR publishing container {creation_id}: {publish_res}")
        return False
    return True


def _post_single_image(image_url, caption, alt_text):
    creation_id = _create_media_container({
        'image_url': image_url, 'caption': caption, 'alt_text': alt_text, 'access_token': ACCESS_TOKEN
    })
    if not creation_id or not _wait_until_ready(creation_id):
        return False
    print("Publishing to Explore...")
    if _publish_container(creation_id):
        print("SUCCESS: published!")
        return True
    return False


def _post_carousel(image_urls, caption, alt_text):
    child_ids = []
    for url in image_urls:
        child_id = _create_media_container({
            'image_url': url, 'is_carousel_item': 'true', 'alt_text': alt_text, 'access_token': ACCESS_TOKEN
        })
        if not child_id:
            print("WARNING: failed to create a carousel slide, falling back to single-image post...")
            return _post_single_image(image_urls[0], caption, alt_text)
        child_ids.append(child_id)

    creation_id = _create_media_container({
        'media_type': 'CAROUSEL', 'children': ",".join(child_ids),
        'caption': caption, 'access_token': ACCESS_TOKEN
    })
    if not creation_id or not _wait_until_ready(creation_id):
        return False
    print("Publishing to Explore...")
    if _publish_container(creation_id):
        print("SUCCESS: published!")
        return True
    return False


def post_to_instagram(image_urls, caption, alt_text):
    """ينشر صورة واحدة أو كاروسيل (لرفع وقت المكوث بالمنشور) حسب عدد الصور المتوفرة."""
    print("Sending photo(s) to Meta for processing...")
    if len(image_urls) >= 2:
        return _post_carousel(image_urls, caption, alt_text)
    return _post_single_image(image_urls[0], caption, alt_text)


def cleanup_temp_files():
    for path in (TEMP_IMAGE, TEMP_IMAGE_2):
        if os.path.exists(path):
            os.remove(path)


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

    topic_summary, headline, highlight, caption, hook, closing_question = generate_arabic_content(story)
    if not (topic_summary and headline and caption):
        print("ERROR: failed to generate Arabic content. Cancelling cycle.")
        return

    print(f"Raw headline (slide 1): {headline!r}")
    print(f"Raw highlight: {highlight!r}")
    print(f"Raw hook (slide 2): {hook!r}")
    print(f"Raw closing question (slide 2): {closing_question!r}")

    if not generate_cover_image(story, topic_summary, headline, highlight):
        print("ERROR: failed to generate image. Cancelling cycle.")
        return

    public_image_url = upload_to_temp_server(TEMP_IMAGE)
    if not public_image_url:
        print("ERROR: failed to upload to temp host. Cancelling cycle.")
        cleanup_temp_files()
        return

    # الشريحة الثانية (كاروسيل) تزيد وقت المكوث بالمنشور — إذا فشلت لأي سبب ننشر بصورة واحدة بدون توقف
    slide_urls = [public_image_url]
    if build_detail_slide(hook, closing_question):
        detail_url = upload_to_temp_server(TEMP_IMAGE_2)
        if detail_url:
            slide_urls.append(detail_url)

    alt_text = _sanitize_headline(headline)[:100]

    print(f"Posting as {'carousel' if len(slide_urls) >= 2 else 'single image'} ({len(slide_urls)} slide(s)).")
    print(f"Full caption:\n{caption}")

    if post_to_instagram(slide_urls, caption, alt_text):
        save_seen_story(story["id"])

    cleanup_temp_files()


# --- نقطة انطلاق النظام الآلي ---
if __name__ == "__main__":
    # تشغيل سيرفر النبض في الخلفية
    server_thread = Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # جدولة النشر: 10 منشورات يومياً موزّعة على أوقات الذروة بالتوقيت المحلي العراقي (UTC+3)
    # كثافة أعلى بالفترة المسائية والليلية (الأعلى تفاعلاً عادة لجمهورنا)، مع حضور صباحي وظهري خفيف.
    PEAK_TIMES_UTC = [
        "06:00",  # 09:00 صباحاً محلي
        "08:00",  # 11:00 صباحاً محلي
        "10:00",  # 01:00 ظهراً محلي
        "12:00",  # 03:00 عصراً محلي
        "13:30",  # 04:30 عصراً محلي
        "15:00",  # 06:00 مساءً محلي
        "16:30",  # 07:30 مساءً محلي
        "18:00",  # 09:00 مساءً محلي (ذروة)
        "19:30",  # 10:30 مساءً محلي (ذروة)
        "21:00",  # 12:00 منتصف الليل محلي (ذروة سهر)
    ]
    for t in PEAK_TIMES_UTC:
        schedule.every().day.at(t).do(job)

    print("Automation running in the background... publishing at the scheduled times.")
    print("Keep-alive server is running; you can now link it with UptimeRobot.")

    # حلقة لانهائية لتبقي السكربت يراقب الوقت
    while True:
        schedule.run_pending()
        time.sleep(60)
