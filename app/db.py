"""SQLite storage — stdlib only, WAL mode."""
import os
import json
import sqlite3
import time
import secrets
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(
        """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        watcha_id INTEGER UNIQUE,
        nickname TEXT, avatar TEXT, email TEXT, phone TEXT,
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER, created_at REAL
    );
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, title_zh TEXT, category TEXT, raw_json TEXT, image TEXT,
        created_at REAL
    );
    CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, user_id INTEGER, product_id INTEGER, market TEXT,
        status TEXT, steps TEXT, listing_slug TEXT, error TEXT, created_at REAL, updated_at REAL
    );
    CREATE TABLE IF NOT EXISTS listings(
        slug TEXT PRIMARY KEY, product_id INTEGER, market TEXT,
        copy_json TEXT, images_json TEXT, insight TEXT,
        views INTEGER DEFAULT 0, created_at REAL
    );
    CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT, stars INTEGER, comment TEXT, name TEXT, created_at REAL
    );
    """
    )
    c.commit()


# ---- users / sessions ----
def upsert_user(watcha_id, nickname, avatar, email=None, phone=None):
    c = conn()
    c.execute(
        """INSERT INTO users(watcha_id,nickname,avatar,email,phone,created_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(watcha_id) DO UPDATE SET nickname=excluded.nickname, avatar=excluded.avatar,
           email=COALESCE(excluded.email,users.email), phone=COALESCE(excluded.phone,users.phone)""",
        (watcha_id, nickname, avatar, email, phone, time.time()),
    )
    c.commit()
    return c.execute("SELECT * FROM users WHERE watcha_id=?", (watcha_id,)).fetchone()


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    conn().execute("INSERT INTO sessions VALUES(?,?,?)", (token, user_id, time.time()))
    conn().commit()
    return token


def user_by_session(token):
    if not token:
        return None
    c = conn()
    r = c.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,)
    ).fetchone()
    return r


def del_session(token):
    conn().execute("DELETE FROM sessions WHERE token=?", (token,))
    conn().commit()


# ---- products ----
def add_product(source, title_zh, category, raw_json, image):
    c = conn()
    cur = c.execute(
        "INSERT INTO products(source,title_zh,category,raw_json,image,created_at) VALUES(?,?,?,?,?,?)",
        (source, title_zh, category, raw_json, image, time.time()),
    )
    c.commit()
    return cur.lastrowid


def list_products():
    return conn().execute("SELECT * FROM products ORDER BY id").fetchall()


def get_product(pid):
    return conn().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


# ---- jobs ----
def job_new(job_id, user_id, product_id, market, steps):
    conn().execute(
        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, user_id, product_id, market, "running", json.dumps(steps), None, None, time.time(), time.time()),
    )
    conn().commit()


def job_update(job_id, **kw):
    c = conn()
    sets, vals = [], []
    for k, v in kw.items():
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k == "steps" else v)
    vals += [time.time(), job_id]
    c.execute(f"UPDATE jobs SET {','.join(sets)}, updated_at=? WHERE id=?", vals)
    c.commit()


def job_get(job_id):
    return conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def jobs_recent(limit=10):
    return conn().execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


# ---- listings ----
def listing_new(slug, product_id, market, copy, images, insight):
    conn().execute(
        "INSERT INTO listings VALUES(?,?,?,?,?,?,0,?)",
        (slug, product_id, market, json.dumps(copy, ensure_ascii=False), json.dumps(images), insight, time.time()),
    )
    conn().commit()


def listing_get(slug, bump=False):
    c = conn()
    if bump:
        c.execute("UPDATE listings SET views=views+1 WHERE slug=?", (slug,))
        c.commit()
    return c.execute("SELECT * FROM listings WHERE slug=?", (slug,)).fetchone()


def listings_all():
    return conn().execute("SELECT * FROM listings ORDER BY created_at DESC").fetchall()


def feedback_add(slug, stars, comment, name):
    conn().execute("INSERT INTO feedback(slug,stars,comment,name,created_at) VALUES(?,?,?,?,?)",
                   (slug, stars, comment, name, time.time()))
    conn().commit()


def feedback_list(slug):
    return conn().execute("SELECT * FROM feedback WHERE slug=? ORDER BY created_at DESC", (slug,)).fetchall()
