"""Regenerate images for an existing listing using product-driven prompts (no copy re-run)."""
import os, sys, json, asyncio, uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

from app import db, llm

GEN_DIR = os.path.join(ROOT, "app", "static", "generated")

async def main(slug):
    db.init()
    l = db.listing_get(slug)
    if not l:
        print("listing not found"); return
    copy = json.loads(l["copy_json"])
    ptitle = copy.get("title", "product")
    brand = copy.get("brand", "the brand")
    feats = []
    for b in copy.get("bullets", []):
        w = " ".join(str(b).split()[:5]).strip(".,;:")
        if w:
            feats.append(w)
        if len(feats) >= 4:
            break
    feat_txt = "', '".join(feats) if feats else "Premium Quality', 'Easy to Style"
    prompts = [
        ("hero", "1024*1024",
         f"Professional e-commerce hero shot of: {ptitle}. "
         "The exact product elegantly displayed on a clean warm ivory background, "
         "soft studio lighting, subtle shadow, photorealistic product photography, "
         "high-end marketplace main image style, centered composition, no text, no watermark"),
        ("lifestyle", "1024*1280",
         f"Lifestyle photo for {brand}: an elegant model wearing or using the exact product '{ptitle}', "
         "in a sunlit modern city street, golden hour, shallow depth of field, "
         "editorial fashion photography, warm film tones, no text, no watermark"),
        ("infographic", "1024*1024",
         f"E-commerce infographic image for: {ptitle}. Flat lay of the exact product with elegant thin "
         f"callout lines and short crisp English labels reading '{feat_txt}'. "
         "Clean minimal layout, ivory and deep green palette, small sharp English text, premium brand aesthetic"),
    ]
    batch = uuid.uuid4().hex[:8]
    images = json.loads(l["images_json"])
    for key, size, prompt in prompts:
        try:
            url = await llm.gen_image(prompt, size=size)
            dest = os.path.join(GEN_DIR, f"{batch}_{key}.png")
            await llm.download(url, dest)
            images[key] = f"/static/generated/{batch}_{key}.png"
            print("ok", key)
        except Exception as e:
            print("FAIL", key, str(e)[:200])
    c = db.conn()
    c.execute("UPDATE listings SET images_json=? WHERE slug=?",
              (json.dumps(images), slug))
    c.commit()
    print("updated", slug, images)

asyncio.run(main(sys.argv[1]))
