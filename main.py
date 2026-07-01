import os
import re
import json
import time
import random
import base64
import subprocess
from threading import Thread

import arabic_reshaper
import feedparser
import requests
import schedule
from bidi.algorithm import get_display
from flask import Flask
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

# --- المفاتيح من بيئة ريبليت ---
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

SEEN_FILE = "seen_stories.json"
TEMP_IMAGE = "temp_image.jpg"
TEMP_VIDEO = "output_reel.mp4"

VIDEO_WIDTH = 1024
VIDEO_HEIGHT = 1792
REEL_DURATION_SECONDS = 8

# قالب العلامة التجارية الثابت (يُطبَّق برمجياً على كل صورة، بدل الاعتماد فقط على طلب GPT)
BRAND_NAME = "RADAR NEWS"
BRAND_ACCENT_COLOR = (255, 196, 60)
BRAND_FRAME_WIDTH = 14
BADGE_TEXT = "أخبار تقنية"
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "music")
BRAND_FONT_PATH = os.path.join(ASSETS_DIR, "LiberationSans-Bold.ttf")
HEADLINE_FONT_PATH = os.path.join(ASSETS_DIR, "NotoKufiArabic-Bold.ttf")

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

# هوية بصرية ثابتة تُضاف لكل صورة غلاف، عشان يكون فيه "تيمبلت" مرئي موحّد يميّز صفحة "رادار نيوز"
BRAND_VISUAL_STYLE = (
    "signature visual identity: moody, atmospheric dark background (deep navy/black) with vivid glowing "
    "neon-blue, purple, and gold accent lighting, rich saturated colors, high contrast, cinematic depth "
    "(never muddy, flat, or desaturated), futuristic tech/AI-inspired digital-art style, "
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


def _shape_arabic_fallback(text):
    """يشكّل النص العربي يدوياً (بدون الحاجة لمكتبة raqm) لضمان عمله بأي بيئة."""
    return get_display(arabic_reshaper.reshape(text))


def _basic_layout_font(font):
    """يعيد تحميل نفس الخط بمحرك تخطيط أساسي، لمنع raqm (إن وُجد) من إعادة تشكيل نص مُشكَّل مسبقاً."""
    try:
        return ImageFont.truetype(font.path, font.size, layout_engine=ImageFont.Layout.BASIC)
    except Exception:
        return font


_ARABIC_FONT_HEALTHY = None


def _arabic_font_healthy(font_path):
    """يتحقق فعلياً (لا يفترض فقط) أن الخط يرسم حرفاً عربياً حقيقياً، لأن raqm أحياناً يفشل بصمت بدون استثناء."""
    global _ARABIC_FONT_HEALTHY
    if _ARABIC_FONT_HEALTHY is not None:
        return _ARABIC_FONT_HEALTHY
    try:
        test_font = ImageFont.truetype(font_path, 60, layout_engine=ImageFont.Layout.BASIC)
        test_img = Image.new("L", (120, 80), 0)
        test_draw = ImageDraw.Draw(test_img)
        test_draw.text((10, 10), _shape_arabic_fallback("الف"), font=test_font, fill=255)
        _ARABIC_FONT_HEALTHY = test_img.getbbox() is not None
        if not _ARABIC_FONT_HEALTHY:
            print(f"ERROR: Arabic font at {font_path} produced no visible glyphs (likely a corrupted/unreadable font file).")
    except Exception as e:
        _ARABIC_FONT_HEALTHY = False
        print(f"ERROR: could not load Arabic font at {font_path}: {str(e)[:200]}")
    return _ARABIC_FONT_HEALTHY


def _raqm_renders_correctly(draw, font):
    """يتحقق أن raqm (إن وُجد) ينتج فعلاً رسماً صحيحاً، بدل الاعتماد على استثناء قد لا يُرفع."""
    try:
        bbox = draw.textbbox((0, 0), "الف", font=font, direction="rtl", language="ar")
        return bool(bbox) and (bbox[2] - bbox[0]) > 5
    except Exception:
        return False


def _arabic_textbbox(draw, text, font):
    if not _arabic_font_healthy(getattr(font, "path", HEADLINE_FONT_PATH)):
        return draw.textbbox((0, 0), text, font=font)
    if _raqm_renders_correctly(draw, font):
        return draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    return draw.textbbox((0, 0), _shape_arabic_fallback(text), font=_basic_layout_font(font))


def _draw_arabic_text(draw, xy, text, font, fill, anchor=None, align="center", stroke_width=0, stroke_fill=None):
    if not _arabic_font_healthy(getattr(font, "path", HEADLINE_FONT_PATH)):
        draw.text(xy, text, font=font, fill=fill, anchor=anchor, align=align,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)
        return
    if _raqm_renders_correctly(draw, font):
        draw.text(xy, text, font=font, fill=fill, direction="rtl", language="ar", anchor=anchor, align=align,
                   stroke_width=stroke_width, stroke_fill=stroke_fill)
    else:
        draw.text(xy, _shape_arabic_fallback(text), font=_basic_layout_font(font), fill=fill, anchor=anchor,
                   align=align, stroke_width=stroke_width, stroke_fill=stroke_fill)


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
    2. لا تُعِد صياغة العنوان الرئيسي المعروف للخبر. ابحث داخل الملخص عن أغرب وأهم تفصيل تقني فيه (رقم صادم، قدرة جديدة غير متوقعة، تأثير مستقبلي)، واجعله محور المحتوى بدل الفكرة العامة المتوقعة.
    3. العنوان المصوّر يجب أن يكون قصيراً جداً (3 إلى 6 كلمات فقط) وصادماً/فضولياً، لأنه سيُكتب بخط كبير مباشرة على الصورة، ويجب أن يُفهم المعنى الأساسي منه وحده بدون أي نص آخر. لا تستخدم علامة التعجب "!" إطلاقاً.
    4. اختم بسؤال حقيقي يحفّز الناس يكتبون تعليق (مو مجرد "شنو رأيك" عام، خليه سؤال مرتبط تحديداً بتفصيل الخبر).
    5. اكتب بالعربية الفصحى المبسطة والسليمة 100%، بدون كلمات إنجليزية.

    استخدم هذا التنسيق بالضبط في ردك (بدون أي نص إضافي خارج هذا التنسيق):
    الملخص: (كلمتين إلى ثلاث كلمات تلخص المشهد البصري الأنسب للتفصيل النادر، لتوليد صورة عنه)
    العنوان: (3 إلى 6 كلمات فقط، يُكتب بخط كبير على الصورة، بدون علامة تعجب)
    الخطاف: (أول جملة بالمنشور، سؤال أو جملة صادمة قصيرة)
    الجسم: (3 إلى 4 جمل تكشف التفصيل النادر تدريجياً بأسلوب قصصي شيق)
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
        hook = _extract_field(full_text, "الخطاف") or headline
        body = _extract_field(full_text, "الجسم") or summary[:300]
        closing_question = _extract_field(full_text, "السؤال الختامي") or "شنو رأيكم؟ 👇"
        hashtags = _extract_field(full_text, "الهاشتاقات") or "#رادار_نيوز #RadarNews"

        # تيمبلت ثابت لصفحة "رادار نيوز" — نفس البنية تتكرر بكل منشور لبناء هوية مميزة للصفحة
        caption = (
            f"{hook}\n\n"
            f"{body}\n\n"
            f"💬 {closing_question}\n"
            f"🔁 شير المنشور لصديق مهووس بالتقنية\n\n"
            f"{hashtags} #رادار_نيوز #RadarNews"
        )

        print(f"Content written: {headline}")
        return topic_summary, headline, caption
    except Exception as e:
        print(f"ERROR generating content: {e}")
        return None, None, None


# --- توليد صورة غلاف عمودية (9:16) آمنة السياسة عبر DALL-E 3 ---
def build_image_prompt(story, topic_summary):
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert at writing DALL-E 3 image prompts for vertical Instagram Reels covers. "
                    "Your prompts are vivid, visual, and always policy-safe, written entirely in English. "
                    "Never include violence, blood, real people, political figures, "
                    "logos, or copyrighted elements. Focus on scene, atmosphere, and concept. "
                    "Never ask the model to render any text, letters, numbers, signs, or writing in the "
                    "image, in any language or script — describe pure visuals only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write a single DALL-E 3 image prompt for a vertical (9:16) Instagram Reel cover about this news story. "
                    f"Translate the concept below into a purely visual English scene description — do not quote or embed "
                    f"the original Arabic text anywhere in the prompt.\n\n"
                    f"Core concept (Arabic, translate to a visual scene only): {topic_summary}\n"
                    f"Title: {title}\n"
                    f"Summary: {summary[:500]}\n\n"
                    f"Requirements:\n"
                    f"- Highly conceptual, surreal, cinematic illustration\n"
                    f"- Vertical composition (9:16)\n"
                    f"- Leave the upper-middle third of the frame relatively uncluttered (simple sky/background there), "
                    f"since bold headline text will be overlaid there afterward\n"
                    f"- ABSOLUTELY NO text, letters, numbers, symbols, signage, or writing anywhere in the image, "
                    f"in any language — this is critical, image models often render garbled text so avoid it entirely\n"
                    f"- Do NOT represent abstract ideas (math, science, data, language, time) using floating digits, "
                    f"equations, glyphs, or symbol clusters, even as decorative or stylized elements — express the "
                    f"concept only through concrete objects, creatures, scenery, colors, and lighting\n"
                    f"- Masterpiece quality, highly detailed, cinematic lighting\n"
                    f"- No real people, no logos, no copyrighted elements\n"
                    f"- Apply this {BRAND_VISUAL_STYLE}\n"
                    f"- Output the prompt only, no explanation"
                ),
            },
        ],
        temperature=0.7,
        max_tokens=220,
    )

    return response.choices[0].message.content.strip()


def _save_generated_image(image_response, headline):
    data = image_response.data[0]
    if getattr(data, "url", None):
        img_data = requests.get(data.url).content
    else:
        img_data = base64.b64decode(data.b64_json)
    with open(TEMP_IMAGE, "wb") as handler:
        handler.write(img_data)
    apply_brand_template(TEMP_IMAGE, headline)


def _sanitize_headline(text):
    return text.replace("!", "").replace("！", "").strip()


def apply_brand_template(image_path, headline):
    """يطبّق هوية بصرية ثابتة (تفتيح + عنوان كبير + شارة + إطار + شعار) على كل صورة."""
    try:
        img = Image.open(image_path).convert("RGB")

        # تصحيح تلقائي للسطوع والتباين والتشبع لضمان ألوان حيوية غير باهتة
        img = ImageEnhance.Brightness(img).enhance(1.05)
        img = ImageEnhance.Contrast(img).enhance(1.12)
        img = ImageEnhance.Color(img).enhance(1.2)

        img = img.convert("RGBA")
        width, height = img.size

        # طبقة داكنة خفيفة فوق كامل الصورة لضمان وضوح النص أياً كانت خلفية الذكاء الاصطناعي
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 60))
        img.alpha_composite(overlay)

        draw = ImageDraw.Draw(img)

        # شارة تصنيف أعلى الصورة
        badge_font = ImageFont.truetype(HEADLINE_FONT_PATH, max(22, width // 34))
        badge_h = int(width // 34 * 2.2)
        bbox = _arabic_textbbox(draw, BADGE_TEXT, badge_font)
        bw = bbox[2] - bbox[0]
        pad = 26
        bx0 = width // 2 - bw // 2 - pad
        bx1 = width // 2 + bw // 2 + pad
        by0 = int(height * 0.055)
        by1 = by0 + badge_h
        draw.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) // 2,
                                outline=BRAND_ACCENT_COLOR + (255,), width=3, fill=(15, 12, 10, 160))
        _draw_arabic_text(draw, ((bx0 + bx1) // 2, (by0 + by1) // 2), BADGE_TEXT, badge_font,
                           BRAND_ACCENT_COLOR + (255,), anchor="mm")

        # العنوان الكبير المكتوب مباشرة على الصورة (المعلومة الأساسية للمشاهد الصامت)
        headline_text = _sanitize_headline(headline)
        headline_font = ImageFont.truetype(HEADLINE_FONT_PATH, max(50, width // 11))
        max_text_width = int(width * 0.86)
        lines = _wrap_arabic_text(draw, headline_text, headline_font, max_text_width)

        line_height = int(width // 11 * 1.25)
        total_height = line_height * len(lines)
        y_cursor = int(height * 0.30) - total_height // 2
        stroke_w = max(2, width // 250)
        for line in lines:
            _draw_arabic_text(draw, (width // 2, y_cursor), line, headline_font, (255, 255, 255, 255),
                               anchor="ma", align="center", stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
            y_cursor += line_height

        # تدرّج داكن أسفل الصورة لوضوح الشعار
        gradient_height = int(height * 0.20)
        gradient = Image.new("RGBA", (width, gradient_height), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        for y in range(gradient_height):
            alpha = int(190 * (y / gradient_height))
            gradient_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        img.alpha_composite(gradient, (0, height - gradient_height))

        draw = ImageDraw.Draw(img)

        # إطار ثابت بلون العلامة التجارية يميّز شكل منشورات الصفحة
        draw.rectangle([0, 0, width - 1, height - 1], outline=BRAND_ACCENT_COLOR + (255,), width=BRAND_FRAME_WIDTH)

        # شعار نصي إنكليزي ثابت (باللاتيني عمداً لتفادي تشوّه الحروف العربية بالصور)
        wordmark_size = max(30, width // 16)
        wordmark_font = ImageFont.truetype(BRAND_FONT_PATH, wordmark_size)
        margin = BRAND_FRAME_WIDTH + 26
        draw.text((margin, height - margin - wordmark_size), BRAND_NAME, font=wordmark_font, fill=(255, 255, 255, 255))

        img.convert("RGB").save(image_path, "JPEG", quality=95)
    except Exception as e:
        print(f"WARNING: could not apply brand template to image (continuing without it): {str(e)[:200]}")


def generate_cover_image(story, topic_summary, headline):
    print("Building a safe image prompt...")
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
        _save_generated_image(image_response, headline)
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
        _save_generated_image(image_response, headline)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"WARNING: gpt-image-1 refused this image due to content policy: {error_msg[:200]}")
        else:
            print(f"ERROR generating image via gpt-image-1: {error_msg[:200]}")
        return False


def _pick_music_track():
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith((".mp3", ".m4a", ".wav"))]
    if not tracks:
        return None
    return os.path.join(MUSIC_DIR, random.choice(tracks))


def create_video_reel():
    print("Assembling video and motion effect...")
    music_path = _pick_music_track()
    if music_path:
        print(f"Using background music: {os.path.basename(music_path)}")
        audio_inputs = ["-stream_loop", "-1", "-i", music_path]
        audio_filter = ["-af", f"afade=t=out:st={REEL_DURATION_SECONDS - 1}:d=1,volume=0.5"]
    else:
        print("NOTE: no background music found in assets/music/. Add royalty-free .mp3 tracks there for audio - publishing a silent video for now.")
        audio_inputs = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        audio_filter = []

    try:
        command = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", "30", "-i", TEMP_IMAGE,
            *audio_inputs,
            "-vf", f"zoompan=z='min(zoom+0.0007,1.2)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d={REEL_DURATION_SECONDS * 30}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps=30",
            *audio_filter,
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-t", str(REEL_DURATION_SECONDS),
            TEMP_VIDEO,
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"ERROR assembling video: {e}")
        return False


def upload_to_temp_server():
    print("Uploading to temporary host (tmpfiles.org)...")
    try:
        with open(TEMP_VIDEO, "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": f}).json()
        return res['data']['url'].replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception as e:
        print(f"ERROR uploading video: {e}")
        return None


def post_reel_to_instagram(video_url, caption):
    print("Sending Reel to Meta for processing...")
    url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media"

    params = {'video_url': video_url, 'caption': caption, 'media_type': 'REELS', 'access_token': ACCESS_TOKEN}
    res = requests.post(url, data=params).json()

    if 'id' in res:
        creation_id = res['id']
        status_url = f"https://graph.facebook.com/v25.0/{creation_id}?fields=status_code&access_token={ACCESS_TOKEN}"

        is_ready = False
        for _ in range(15):
            time.sleep(10)
            status_res = requests.get(status_url).json()
            if status_res.get('status_code') == 'FINISHED':
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
    for path in (TEMP_IMAGE, TEMP_VIDEO):
        if os.path.exists(path):
            os.remove(path)


def job():
    """دورة الإنتاج الآلية: جلب خبر حقيقي -> محتوى عربي -> صورة بعنوان مكتوب -> فيديو بموسيقى -> نشر"""
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

    topic_summary, headline, caption = generate_arabic_content(story)
    if not (topic_summary and headline and caption):
        print("ERROR: failed to generate Arabic content. Cancelling cycle.")
        return

    if not generate_cover_image(story, topic_summary, headline):
        print("ERROR: failed to generate image. Cancelling cycle.")
        return

    if not create_video_reel():
        print("ERROR: failed to assemble video. Cancelling cycle.")
        cleanup_temp_files()
        return

    public_video_url = upload_to_temp_server()
    if not public_video_url:
        print("ERROR: failed to upload to temp host. Cancelling cycle.")
        cleanup_temp_files()
        return

    if post_reel_to_instagram(public_video_url, caption):
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
