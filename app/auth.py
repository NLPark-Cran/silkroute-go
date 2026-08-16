"""观猹 Watcha OAuth2 (authorization code, confidential client)."""
import os
import secrets
import httpx
from urllib.parse import quote

CLIENT_ID = os.environ.get("WATCHA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WATCHA_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:18100")
AUTHORIZE_URL = "https://watcha.cn/oauth/authorize"
TOKEN_URL = "https://watcha.cn/oauth/api/token"
USERINFO_URL = "https://watcha.cn/oauth/api/userinfo"
_states = set()


def login_redirect():
    state = secrets.token_urlsafe(16)
    _states.add(state)
    redirect_uri = f"{BASE_URL}/auth/callback"
    url = (
        f"{AUTHORIZE_URL}?response_type=code"
        f"&client_id={quote(CLIENT_ID, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scope=read&state={state}"
    )
    return url, state


def check_state(state):
    ok = state in _states
    _states.discard(state)
    return ok


async def exchange_code(code):
    redirect_uri = f"{BASE_URL}/auth/callback"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=data,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json()


async def userinfo(access_token):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(USERINFO_URL, params={"access_token": access_token})
        r.raise_for_status()
        d = r.json()
        if d.get("statusCode") == 200:
            return d["data"]
        raise RuntimeError(str(d))
