import os

# load .env before anything else
_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env):
    for line in open(_env):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

import uuid
import time
import json
import asyncio
import io
import base64

import qrcode
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, auth, pipeline, seed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
BASE_URL = os.environ.get("BASE_URL", "http://localhost:18100")

app = FastAPI(title="SilkRoute Go — 一键出海 Agent", docs_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
_pitch = os.path.join(ROOT_DIR, "pitch")
if os.path.isdir(_pitch):
    app.mount("/pitch", StaticFiles(directory=_pitch, html=True), name="pitch")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.on_event("startup")
def startup():
    db.init()
    seed.seed(os.path.join(ROOT_DIR, "task_data", "Task_Data", "Data_for_Users(2)"))


def current_user(request: Request):
    return db.user_by_session(request.cookies.get("sid"))


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {
        "user": current_user(request),
        "listings": db.listings_all(),
    })


@app.get("/studio", response_class=HTMLResponse)
def studio(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login/watcha")
    return templates.TemplateResponse(request, "studio.html", {
        "user": user,
        "products": db.list_products(),
        "markets": pipeline.MARKETS,
        "jobs": db.jobs_recent(),
    })


@app.get("/p/{slug}", response_class=HTMLResponse)
def shelf(request: Request, slug: str):
    l = db.listing_get(slug, bump=True)
    if not l:
        return HTMLResponse("<h1>404 — listing not found</h1>", status_code=404)
    copy = json.loads(l["copy_json"])
    images = json.loads(l["images_json"])
    qr = qrcode.make(f"{BASE_URL}/p/{slug}")
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    fb = db.feedback_list(slug)
    avg = round(sum(f["stars"] for f in fb) / len(fb), 1) if fb else None
    return templates.TemplateResponse(request, "shelf.html", {
        "l": l, "copy": copy, "images": images,
        "qr_b64": qr_b64, "feedback": fb, "avg": avg,
        "market": pipeline.MARKETS.get(l["market"], pipeline.MARKETS["us"]),
        "insight": l["insight"] or "",
    })


# ---------- auth ----------
@app.get("/login/watcha")
def login():
    url, _ = auth.login_redirect()
    return RedirectResponse(url)


@app.get("/auth/callback")
async def callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return HTMLResponse(f"<h1>授权失败</h1><p>{error}: {error_description}</p><a href='/'>返回</a>", status_code=400)
    token_data = await auth.exchange_code(code)
    info = await auth.userinfo(token_data["access_token"])
    user = db.upsert_user(info["user_id"], info.get("nickname", "观猹用户"),
                          info.get("avatar_url", ""), info.get("email"), info.get("phone"))
    sid = db.new_session(user["id"])
    resp = RedirectResponse("/studio")
    resp.set_cookie("sid", sid, httponly=True, max_age=86400 * 7, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    db.del_session(request.cookies.get("sid"))
    resp = RedirectResponse("/")
    resp.delete_cookie("sid")
    return resp


# ---------- api ----------
@app.post("/api/generate")
async def generate(request: Request, product_id: int = Form(...), market: str = Form("us")):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required"}, status_code=401)
    job_id = uuid.uuid4().hex[:12]
    db.job_new(job_id, user["id"], product_id, market, pipeline.STEPS_TEMPLATE)
    pipeline.launch(job_id, product_id, market)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    j = db.job_get(job_id)
    if not j:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": j["id"], "status": j["status"], "steps": json.loads(j["steps"]),
        "listing_slug": j["listing_slug"], "error": j["error"],
    }


@app.post("/api/feedback/{slug}")
async def feedback(slug: str, stars: int = Form(...), comment: str = Form(""), name: str = Form("")):
    if not db.listing_get(slug):
        return JSONResponse({"error": "not found"}, status_code=404)
    stars = max(1, min(5, stars))
    db.feedback_add(slug, stars, (comment or "")[:300], (name or "Anonymous")[:40])
    return {"ok": True}
