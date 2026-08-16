"""LLM / image generation clients — DashScope (千问AI平台) via OpenAI-compatible & multimodal-generation APIs."""
import os
import json
import httpx

DASH_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
IMG_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
TEXT_MODEL = "qwen3.8-max"
TEXT_MODEL_FALLBACK = "qwen3.7-plus"
IMAGE_MODEL = "qwen-image-3.0-pro"


async def chat(messages, model=TEXT_MODEL, temperature=0.7, json_mode=False, max_tokens=4096):
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=240) as c:
        r = await c.post(CHAT_URL, headers={"Authorization": f"Bearer {DASH_KEY}"}, json=body)
        if r.status_code != 200 and model != TEXT_MODEL_FALLBACK:
            body["model"] = TEXT_MODEL_FALLBACK
            r = await c.post(CHAT_URL, headers={"Authorization": f"Bearer {DASH_KEY}"}, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def chat_json(messages, **kw):
    kw["json_mode"] = True
    txt = await chat(messages, **kw)
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(txt)


async def gen_image(prompt, size="1024*1024"):
    """Returns image URL."""
    body = {
        "model": IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"size": size, "prompt_extend": True, "watermark": False},
    }
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(
            IMG_URL,
            headers={"Authorization": f"Bearer {DASH_KEY}", "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        d = r.json()
        choices = d.get("output", {}).get("choices", [])
        for ch in choices:
            for part in ch.get("message", {}).get("content", []):
                if "image" in part:
                    return part["image"]
        raise RuntimeError(f"no image in response: {json.dumps(d)[:500]}")


async def download(url, dest):
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
    return dest
