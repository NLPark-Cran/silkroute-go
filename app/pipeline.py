"""Go-global agent pipeline: insight -> copy -> visuals -> publish."""
import os
import json
import asyncio
import traceback

from . import llm, db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
GEN_DIR = os.path.join(STATIC_DIR, "generated")

MARKETS = {
    "us": {"name": "美国 / North America", "lang": "English", "style": "Amazon + Etsy 货架风格"},
    "jp": {"name": "日本", "lang": "Japanese", "style": "侘寂极简"},
    "sea": {"name": "东南亚", "lang": "English", "style": "TikTok Shop 短平快"},
    "me": {"name": "中东", "lang": "English + Arabic", "style": "奢华调性"},
}

STEPS_TEMPLATE = [
    {"key": "insight", "label": "市场洞察", "status": "pending", "detail": ""},
    {"key": "copy", "label": "多语言货架文案", "status": "pending", "detail": ""},
    {"key": "visuals", "label": "视觉素材生成", "status": "pending", "detail": ""},
    {"key": "publish", "label": "Go Live 上架", "status": "pending", "detail": ""},
]


def _slugify(text):
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "item"


async def run_pipeline(job_id, product_id, market):
    steps = [dict(s) for s in STEPS_TEMPLATE]

    def set_step(i, status, detail=""):
        steps[i]["status"] = status
        steps[i]["detail"] = detail
        db.job_update(job_id, steps=steps)

    try:
        product = db.get_product(product_id)
        if not product:
            raise RuntimeError("product not found")
        raw = {}
        try:
            raw = json.loads(product["raw_json"] or "{}")
        except Exception:
            pass
        m = MARKETS.get(market, MARKETS["us"])
        title_zh = product["title_zh"]
        category = product["category"] or ""

        # Step 1: market insight
        set_step(0, "running", f"{m['name']} 市场分析中…")
        insight = await llm.chat([
            {"role": "system", "content": "You are a cross-border e-commerce strategist. Be concrete and concise, output in Chinese with key English terms."},
            {"role": "user", "content": (
                f"商品：{title_zh}（类目：{category}）。目标市场：{m['name']}。"
                "请给出 4-6 条可落地的市场洞察：目标人群画像、价格带建议、卖点切入角度、"
                "竞品差异化机会、内容营销渠道建议。每条一句话，Markdown 列表输出。")},
        ], max_tokens=1200)
        set_step(0, "done", "市场洞察完成")

        # Step 2: listing copy (structured)
        set_step(1, "running", f"生成 {m['lang']} 货架文案…")
        copy = await llm.chat_json([
            {"role": "system", "content": "You are a world-class e-commerce copywriter for cross-border DTC brands. Output STRICT JSON only."},
            {"role": "user", "content": (
                f"Create a product listing for the {m['name']} market in {m['lang']} "
                f"(brand voice: {m['style']}).\n"
                f"Product (Chinese source): {title_zh}, category: {category}.\n"
                "Return JSON with keys: brand (English brand name), title (<=160 chars), subtitle, "
                "bullets (array of 5 strings), description (2-3 paragraphs, use \\n\\n), "
                "keywords (array of 7 SEO keywords), price_usd (number), compare_at_usd (number), "
                "instagram_caption (string with hashtags), story (2-sentence brand story about Hangzhou craftsmanship).")},
        ], max_tokens=2500)
        set_step(1, "done", f"文案完成：{copy.get('title', '')[:60]}")

        # Step 3: visuals
        set_step(2, "running", "qwen-image-3.0-pro 生成主图/场景图/卖点图…")
        os.makedirs(GEN_DIR, exist_ok=True)
        img_prompts = [
            ("hero", "1024*1024",
             f"Professional e-commerce hero shot: {copy.get('title','luxury silk scarf')}. "
             "A luxurious Hangzhou mulberry silk scarf elegantly draped and folded on a clean warm ivory background, "
             "soft studio lighting, subtle shadow, photorealistic product photography, high-end Amazon main image style, centered composition"),
            ("lifestyle", "1024*1280",
             f"Lifestyle photo for {copy.get('brand','a silk brand')}: an elegant woman in her 30s wearing a luxurious printed silk scarf, "
             "walking through a sunlit modern city street, golden hour, shallow depth of field, editorial fashion photography, warm film tones"),
            ("infographic", "1024*1024",
             f"E-commerce infographic image for a premium mulberry silk scarf: flat lay of the scarf with elegant thin callout lines and short English labels "
             f"highlighting '100% Mulberry Silk', 'Hand-rolled Edges', '6A Grade', '90 x 90 cm'. Clean minimal layout, ivory and deep green palette, small crisp English text, premium brand aesthetic"),
        ]
        images = {}
        for key, size, prompt in img_prompts:
            try:
                url = await llm.gen_image(prompt, size=size)
                dest = os.path.join(GEN_DIR, f"{job_id}_{key}.png")
                await llm.download(url, dest)
                images[key] = f"/static/generated/{job_id}_{key}.png"
            except Exception as e:
                images[key] = None
                steps[2]["detail"] += f"[{key} 失败: {str(e)[:80]}] "
        if not any(images.values()):
            raise RuntimeError("所有图片生成失败")
        set_step(2, "done", f"生成 {sum(1 for v in images.values() if v)} 张视觉素材")

        # Step 4: publish
        set_step(3, "running", "生成货架页与二维码…")
        base_slug = _slugify(copy.get("title", title_zh))
        slug = base_slug
        n = 1
        while db.listing_get(slug):
            n += 1
            slug = f"{base_slug}-{n}"
        db.listing_new(slug, product_id, market, copy, images, insight)
        set_step(3, "done", f"已上线：/p/{slug}")
        db.job_update(job_id, status="done", listing_slug=slug, steps=steps)
    except Exception as e:
        traceback.print_exc()
        db.job_update(job_id, status="failed", error=str(e)[:500], steps=steps)


def launch(job_id, product_id, market):
    asyncio.create_task(run_pipeline(job_id, product_id, market))
