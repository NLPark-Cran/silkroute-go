"""Seed products: hero silk scarf + Task_Data wholesale clothing items."""
import os
import re
import json
import glob

from . import db

HERO = {
    "title_zh": "西湖烟雨 · 6A级桑蚕丝双面印花丝巾 90×90cm 手工卷边",
    "category": "真丝丝巾 / Silk Scarf",
    "image": "",
    "raw": {
        "material": "100% 6A-grade mulberry silk, 16 momme twill",
        "size": "90 x 90 cm",
        "craft": "hand-rolled edges, double-sided print",
        "origin": "Hangzhou, China — silk capital for 4,700 years",
        "design": "West Lake misty-rain motif, ink-wash inspired",
    },
}


def _first_image(html):
    m = re.search(r"<img[^>]+src='([^']+)'", html or "")
    return m.group(1) if m else ""


def seed(task_data_dir):
    if db.list_products():
        return
    db.add_product("hero", HERO["title_zh"], HERO["category"],
                   json.dumps(HERO["raw"], ensure_ascii=False), HERO["image"])
    for f in sorted(glob.glob(os.path.join(task_data_dir, "product_info", "*.json"))):
        try:
            d = json.load(open(f))
            r = d["ret"]["result"]["result"]
            subject = r.get("subject", "")
            cat = r.get("category_name", "")
            img = _first_image(r.get("description", ""))
            db.add_product("task_data", subject, cat,
                           json.dumps({"offerId": r.get("offerId"), "platform": r.get("platform")},
                                      ensure_ascii=False), img)
        except Exception as e:
            print("seed skip", f, e)
    print("seeded", len(db.list_products()), "products")
