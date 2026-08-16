#!/usr/bin/env python3
"""
SilkRoute Go · 千问Arena 提交版 Agent
=====================================
从商品数据自动生成跨境电商全套上架素材：
  市场洞察 → 三语文案(英/韩/葡) → 主图 + 5张详情图 → 展示视频 → 策略文档

运行方式（平台评测同款）：
  python agent.py --prompt "读取 /input/ 目录下目标商品的全部信息文件，提取指定内容，按规范生成输出文件并保存至 /output/"
  python agent.py --version

相对官方 Baseline 的增强：
  1. 修复图像/视频端点：qwen-image-3.0-pro 走 multimodal-generation（同步），
     happyhorse-1.1-t2v 走 video-generation/video-synthesis（异步）
  2. 修复路径解析正则：支持带括号/中文的目录名（Baseline 会在 "Data(2)" 处截断）
  3. 文本模型升级 qwen3.8-max，带 qwen3.7-plus → qwen3.5-plus 降级链
  4. 新增「市场洞察」前置步骤：先产出目标市场分析，再驱动文案与视觉提示词
  5. 图片逐张重试 + 兜底提示词，尽力保证 6/6 全部产出
  6. 任一关键产出缺失时以非 0 退出码结束（符合平台规范）
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ============================================================
# 全局配置
# ============================================================

AGENT_DIR = Path(__file__).parent
with open(AGENT_DIR / "agent.json", encoding="utf-8") as _f:
    VERSION = json.load(_f)["version"]

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"
).rstrip("/")
OPENAI_BASE = os.environ.get(
    "OPENAI_BASE_URL", f"{DASHSCOPE_BASE}/compatible-mode/v1"
)

# ---- 模型选择 ----
TEXT_MODELS = ["qwen3.8-max", "qwen3.7-plus", "qwen3.5-plus"]  # 降级链
IMAGE_MODEL = "qwen-image-3.0-pro"   # multimodal-generation 同步接口
VIDEO_MODEL = "happyhorse-1.1-t2v"   # video-synthesis 异步接口

IMAGE_ENDPOINT = "/api/v1/services/aigc/multimodal-generation/generation"
VIDEO_ENDPOINT = "/api/v1/services/aigc/video-generation/video-synthesis"
TASK_ENDPOINT = "/api/v1/tasks"

POLL_INTERVAL = 10
POLL_TIMEOUT = 600

# 必须产出的文件（官方交付清单，命名不可改）
REQUIRED_FILES = (
    ["product_description_en.txt", "product_description_ko.txt",
     "product_description_pt.txt", "main_image.png"]
    + [f"detail_image_{i}.png" for i in range(1, 6)]
    + ["product_video.mp4", "strategy_document.txt"]
)

# ============================================================
# 日志
# ============================================================

def setup_logging():
    log_dir = os.environ.get("AGENT_LOG_DIR", str(AGENT_DIR))
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("agent")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(os.path.join(log_dir, "agent.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger

log = setup_logging()


# ============================================================
# 命令行参数 & 路径解析（修复：支持括号/中文字符的路径）
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="SilkRoute Go Agent")
    parser.add_argument("--prompt", type=str, default="")
    parser.add_argument("--version", action="store_true")
    return parser.parse_args()


def extract_paths(prompt):
    """从自然语言 prompt 中提取输入/输出目录。
    兼容：markdown 反引号/引号包裹路径、全角标点、「根据/中的数据」「输出到」等表述。
    官方机测 Prompt 原文：读取 `/home/user/ws/input/` 目录下目标商品的全部信息文件，
    提取指定内容，按规范生成输出文件并保存至 `/home/user/ws/output/`。
    """
    p = (prompt.replace("`", " ").replace("\u2018", " ").replace("\u2019", " ")
               .replace("\u201c", " ").replace("\u201d", " ")
               .replace("'", " ").replace('"', " "))
    inp = re.search(r"(?:读取|输入|根据|基于)[:\s]*(/[^\s，。；]+?)/?\s*(?:目录|中|下)", p)
    out = re.search(r"(?:保存至|保存到|输出到|输出|生成到?)[:\s]*(/[^\s，。；]+?)/?(?:。|$|，|；|\s)", p)
    if inp and out:
        return inp.group(1).rstrip("/") + "/", out.group(1).rstrip("/") + "/"
    # 兜底：按出现顺序取所有绝对路径，第一个输入、最后一个输出
    paths = [x.rstrip("/") for x in re.findall(r"/[^\s，。；`]+", p)]
    if len(paths) >= 2:
        return paths[0] + "/", paths[-1] + "/"
    log.warning("无法从 prompt 中解析路径，使用默认值")
    return "/home/user/ws/input/", "/home/user/ws/output/"


# ============================================================
# 读取商品数据
# ============================================================

def read_input_data(input_dir):
    result = {}
    base = Path(input_dir)
    known = [
        ("product_basic", base / "product_info" / "product_basic.json"),
        ("clothing_attributes", base / "clothing_attributes.json"),
        ("clothing_categories", base / "clothing_categories.json"),
    ]
    for key, path in known:
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    result[key] = json.load(f)
                log.info(f"  读取成功: {path}")
            except Exception as e:
                log.error(f"  读取失败 {path}: {e}")
    for jf in base.rglob("*.json"):
        rel = str(jf.relative_to(base))
        key = rel.replace("/", "__").replace(".json", "")
        if key not in result:
            try:
                with open(jf, encoding="utf-8") as f:
                    result[key] = json.load(f)
                log.info(f"  额外文件: {jf}")
            except Exception:
                pass
    return result


def get_product_title(data):
    keys = ("title", "subject", "name", "product_name", "product_title",
            "商品名称", "标题")

    def dig(d, depth=0):
        """在嵌套 dict 中找商品标题（兼容 1688 风格 ret.result.result 嵌套）。"""
        if not isinstance(d, dict) or depth > 4:
            return None
        for k in keys:
            if k in d and isinstance(d[k], str) and d[k].strip():
                return d[k].strip()
        for v in d.values():
            if isinstance(v, dict):
                r = dig(v, depth + 1)
                if r:
                    return r
        return None

    # 优先 product_basic，其次 product_info 下的商品文件，最后其他数据源
    items = sorted(data.items(), key=lambda kv: 0 if "product_info" in kv[0] else 1)
    for source in [data.get("product_basic")] + [v for _, v in items]:
        t = dig(source)
        if t:
            return t
    return "fashion clothing product"


def brief_product_text(data, limit=8000):
    """把商品数据压缩成给 LLM 的摘要文本。"""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    # 剥掉 description 里的超长 HTML 图片标签，保留文字
    text = re.sub(r"<img[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\r|\\n|\s{2,}", " ", text)
    return text[:limit]


# ============================================================
# 文本模型调用（纯标准库，零三方依赖 → ZIP 无需打包任何依赖）
# 通过 OPENAI_BASE_URL 兼容端点，带降级链与限流重试
# ============================================================

def call_llm(system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
    last_err = None
    for model in TEXT_MODELS:
        for attempt in range(2):
            try:
                body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                req = urllib.request.Request(
                    f"{OPENAI_BASE}/chat/completions",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Authorization": f"Bearer {API_KEY}",
                             "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=240) as r:
                        resp = json.loads(r.read())
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", "ignore")[:300]
                    # 限流（429）等待后重试
                    if e.code == 429 and attempt == 0:
                        log.warning(f"  {model} 限流，20s 后重试")
                        time.sleep(20)
                        continue
                    raise RuntimeError(f"HTTP {e.code}: {detail}")
                text = resp["choices"][0]["message"]["content"] or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                if text:
                    if model != TEXT_MODELS[0]:
                        log.info(f"  （已降级到 {model}）")
                    return text
            except Exception as e:
                log.warning(f"  文本模型 {model} 调用失败: {str(e)[:200]}")
                last_err = e
                break
    raise RuntimeError(f"全部文本模型不可用: {last_err}")


# ============================================================
# DashScope 原生 API 工具
# ============================================================

def _ds_request(path, body, async_mode=False, timeout=300):
    url = f"{DASHSCOPE_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if async_mode:
        headers["X-DashScope-Async"] = "enable"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}")


def poll_task(task_id):
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        url = f"{DASHSCOPE_BASE}{TASK_ENDPOINT}/{task_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        status = result.get("output", {}).get("task_status", "UNKNOWN")
        log.info(f"  轮询 {task_id[:16]}... 状态={status} ({elapsed}s)")
        if status == "SUCCEEDED":
            return result
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise RuntimeError(f"任务异常终止: {json.dumps(result, ensure_ascii=False)[:500]}")
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    raise TimeoutError(f"任务 {task_id} 等待超过 {POLL_TIMEOUT}s")


def extract_result_urls(result):
    output = result.get("output", {})
    urls = []
    for item in output.get("results", []):
        if isinstance(item, dict) and "url" in item:
            urls.append(item["url"])
    if "video_url" in output:
        urls.append(output["video_url"])
    if "url" in output:
        urls.append(output["url"])
    return urls


def download_file(url, save_path):
    if not url:
        return False
    try:
        log.info(f"  下载 -> {os.path.basename(save_path)}")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=180) as r:
            with open(save_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        log.info(f"  完成: {os.path.getsize(save_path):,} bytes")
        return True
    except Exception as e:
        log.error(f"  下载失败: {e}")
        return False


# ============================================================
# 步骤零：市场洞察（增强项）
# ============================================================

def step_insight(product_data):
    """先产出目标市场洞察，后续文案/视觉共用。"""
    title = get_product_title(product_data)
    log.info("[洞察] 目标市场分析中...")
    try:
        return call_llm(
            "你是跨境电商市场策略专家，精通 AliExpress/Amazon/TikTok Shop 运营。"
            "输出务实、具体、可执行的中文分析，每条一句话。",
            f"商品：{title}\n商品数据摘要：{brief_product_text(product_data, 4000)}\n\n"
            "请输出：1) 欧美/韩国/巴西三个市场的目标人群画像；2) 建议价格带（USD/KRW/BRL）；"
            "3) 三个市场各自的卖点切入角度；4) 视觉风格建议（色彩/场景/模特）。",
            max_tokens=1500)
    except Exception as e:
        log.warning(f"  洞察生成失败（不影响主流程）: {e}")
        return ""


# ============================================================
# 步骤一：三语文案（洞察驱动）
# ============================================================

def step_descriptions(product_data, output_dir, insight):
    data_text = brief_product_text(product_data)
    title = get_product_title(product_data)
    # 类目/属性词表：让文案与之精确匹配（评分维度：类目属性 18%）
    cats = json.dumps(product_data.get("clothing_categories", {}), ensure_ascii=False)[:800]
    attrs = json.dumps(product_data.get("clothing_attributes", {}), ensure_ascii=False)[:800]
    market_notes = {
        "English": "面向美国市场（Amazon/Etsy 调性）：强调品质、材质与送礼场景；尺码用英寸（inch）表示并附 US 码换算，价格建议用 USD",
        "Korean": "面向韩国市场：强调时尚感、穿搭建议；尺码用韩国习惯（44/55/66 或 KR 码）表示，价格建议用 KRW",
        "Portuguese": "面向巴西市场：突出性价比、舒适度；尺码用厘米（cm）表示并附 BR 码换算，价格建议用 BRL",
    }
    languages = [
        ("English", "英文", "product_description_en.txt"),
        ("Korean", "韩文", "product_description_ko.txt"),
        ("Portuguese", "葡萄牙文", "product_description_pt.txt"),
    ]
    ok = 0
    for lang, label, filename in languages:
        log.info(f"[文案] 正在生成{label}文案...")
        try:
            system = (
                f"你是跨境电商文案专家。请用纯正地道的{lang}撰写 AliExpress 商品上架文案。"
                "\n【文案结构】商品标题、五点卖点描述、SKU 信息（尺码/颜色等变体及其分解项）、"
                "商品属性（材质/风格/场景）、数据来源平台名称与商品 ID/URL（如数据中有）、"
                "以及配套素材文件名介绍：main_image.png（主图）、detail_image_1~5.png"
                "（详情图）、product_video.mp4（展示视频）。"
                f"\n【本地化】{market_notes[lang]}。"
                "\n【内容合规】遵守 AliExpress 内容政策：禁止使用绝对化用语"
                "（best/No.1/100%/guaranteed/perfect 等），禁止未经证实的功效描述"
                "（抗菌/医疗/减肥等），不使用无法验证的认证宣称。"
                "\n【事实一致】所有材质、产地、规格、尺码必须严格来自输入数据；"
                "数据中没有的信息宁可不写，也禁止虚构。"
                f"\n【类目属性】商品类目与属性词必须从以下平台词表中精确选取，不要自造："
                f"\n类目表：{cats}\n属性表：{attrs}"
                "\n【结构化区块】文案末尾必须用如下固定标签逐行输出（供机器解析，标签本身用英文）："
                "\n[Category] 与平台类目表精确匹配的叶子类目名称"
                "\n[Attributes] key: value 逐行列出商品属性（从平台属性词表取值）"
                "\n[Sale Attributes] 逐行列出销售属性/规格分类值（如 Color: xxx, Size: XL）"
            )
            user = (
                f"商品标题：{title}\n"
                f"市场洞察参考：\n{insight}\n\n"
                f"商品完整数据：\n{data_text}\n\n"
                "请直接输出文案，不要加额外解释。"
            )
            text = call_llm(system, user)
            with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
                f.write(text)
            log.info(f"  已保存: {filename} ({len(text)} 字符)")
            ok += 1
        except Exception as e:
            log.error(f"  {label}文案生成失败: {e}")
    return ok


# ============================================================
# 步骤二：图片（同步接口 + 逐张重试）
# ============================================================

def _gen_image_sync(prompt, size="1024*1024"):
    resp = _ds_request(IMAGE_ENDPOINT, {
        "model": IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"size": size, "prompt_extend": True, "watermark": False},
    })
    for ch in resp.get("output", {}).get("choices", []):
        for part in ch.get("message", {}).get("content", []):
            if "image" in part:
                return part["image"]
    raise RuntimeError(f"响应中无图片: {json.dumps(resp, ensure_ascii=False)[:300]}")


def step_images(product_data, output_dir, insight):
    title = get_product_title(product_data)
    style_hint = ""
    if insight:
        m = re.search(r"视觉风格建议[:：]?(.{0,200})", insight)
        if m:
            style_hint = " Visual style: " + m.group(1)[:150]

    NOISE = ", no text, no watermark, no logo, no label, centered composition"
    specs = [
        ("main_image.png",
         f"{title}, professional e-commerce hero product photo, clean white "
         "background, soft studio lighting, high resolution, photorealistic, "
         "marketplace main image, product fills 85% of frame" + NOISE),
        ("detail_image_1.png",
         f"{title}, front view full display, e-commerce detail photo, "
         "clean background, professional lighting, true colors" + NOISE),
        ("detail_image_2.png",
         f"{title}, alternate angle and styling details, e-commerce photo, "
         "clean background, consistent product appearance" + NOISE),
        ("detail_image_3.png",
         f"{title}, extreme close-up of material texture and craftsmanship, "
         "macro photography, crisp fabric or surface details, no text, no watermark"),
        ("detail_image_4.png",
         f"{title}, lifestyle photo, elegant model using or wearing the product, "
         "natural golden-hour lighting, editorial fashion style, no text, no watermark"),
        ("detail_image_5.png",
         f"{title}, flat lay arrangement with matching accessories, top-down view, "
         "aesthetic premium composition, magazine style, no text, no watermark"),
    ]

    ok = 0
    for filename, prompt in specs:
        saved = False
        for attempt, p in enumerate([prompt + style_hint, prompt]):
            log.info(f"[图片] 生成 {filename}" + (f"（第{attempt+1}次重试·简化提示词）" if attempt else ""))
            try:
                img_url = _gen_image_sync(p)
                if download_file(img_url, os.path.join(output_dir, filename)):
                    saved = True
                    ok += 1
                    break
            except Exception as e:
                log.error(f"  {filename} 失败: {str(e)[:200]}")
                time.sleep(3)
        if not saved:
            log.error(f"  {filename} 重试后仍失败")
    return ok


# ============================================================
# 步骤三：视频（异步接口）
# ============================================================

def step_video(product_data, output_dir):
    title = get_product_title(product_data)
    prompt = (
        f"{title}, premium product showcase video, slow rotating display, "
        "smooth camera orbit, soft studio lighting, elegant e-commerce style, "
        "consistent product appearance, no text, no watermark, no subtitles"
    )
    log.info("[视频] 提交视频生成任务...")
    try:
        resp = _ds_request(VIDEO_ENDPOINT, {
            "model": VIDEO_MODEL,
            "input": {"prompt": prompt},
            "parameters": {},
        }, async_mode=True)
        task_id = resp.get("output", {}).get("task_id")
        if not task_id:
            log.error(f"  视频任务提交异常: {resp}")
            return False
        log.info(f"  任务已提交: {task_id[:16]}...")
        result = poll_task(task_id)
        urls = extract_result_urls(result)
        if urls:
            return download_file(urls[0], os.path.join(output_dir, "product_video.mp4"))
        log.warning("  视频任务完成但未获得产物 URL")
    except Exception as e:
        log.error(f"  视频生成失败: {e}")
    return False


# ============================================================
# 步骤四：策略文档
# ============================================================

def step_strategy(product_data, output_dir, insight, stats):
    title = get_product_title(product_data)
    doc = f"""策略说明文档 · SilkRoute Go Agent v{VERSION}
{'=' * 56}

一、整体方案
本 Agent 采用「洞察驱动」的全自动流水线，为商品「{title}」
生成跨境电商平台（以 AliExpress 为例）上架所需的全套本地化素材。
流程：读取商品数据 → 市场洞察 → 三语文案 → 主图+5张详情图 → 展示视频 → 本策略文档。
与素材包配套的线上系统（SilkRoute Go, https://tabroad.hub.tt2.li）可将本目录素材
一键组装为可公开访问的海外货架页，完成从「素材生成」到「真实上架」的最后一公里。

二、模型选型
- 市场洞察/文案：{TEXT_MODELS[0]}（千问旗舰多模态文本模型；降级链 {" → ".join(TEXT_MODELS[1:])}）
- 图片生成：{IMAGE_MODEL}（multimodal-generation 同步接口；10px 级小字清晰，适合卖点信息图）
- 视频生成：{VIDEO_MODEL}（video-synthesis 异步接口）

三、市场洞察摘要
{insight or '（本次运行未生成，使用默认市场假设）'}

四、文案策略（对标评分维度设计）
- 内容合规：系统提示词内置 AliExpress 内容政策约束——禁止绝对化用语、
  未经证实的功效描述与认证宣称
- 事实一致：所有材质/产地/规格严格来自输入数据，数据没有的信息宁可不写
- 类目属性：商品类目与属性词从 clothing_categories.json 与
  clothing_attributes.json 平台词表中精确选取，不自造
- 本地化适配：英文（美国）用英寸+US 码+USD；韩文用韩国尺码习惯+KRW；
  葡文（巴西）用厘米+BR 码+BRL
- 三个语言版本：英文(en) 欧美 Amazon/Etsy 调性 / 韩文(ko) 韩国时尚穿搭导向 /
  葡语(pt) 巴西性价比导向
所有文案均包含：商品标题、五点卖点、SKU 变体及分解项、商品属性、平台名称/ID、
配套图片与视频文件名及说明。

五、视觉素材策略（全部 1024x1024，无文字无水印无 logo）
- main_image.png    ：纯白底主图，商品占画面 85%，居中构图
- detail_image_1.png：正面完整展示
- detail_image_2.png：侧面/背面与造型细节
- detail_image_3.png：材质工艺微距特写
- detail_image_4.png：模特使用生活场景（黄金时刻自然光）
- detail_image_5.png：平铺搭配俯拍，杂志感构图
- product_video.mp4 ：商品旋转展示短片，环绕运镜

六、可靠性设计
- 零三方依赖：全部使用 Python 标准库实现，ZIP 无需打包依赖，与评测环境完全解耦
- 文本模型三级降级链，429 限流自动等待重试，单模型故障不中断流程
- 每张图片失败自动以简化提示词重试一次
- 任一关键产出缺失时进程以非 0 退出码结束，便于平台判定

七、本次运行统计
- 三语文案成功: {stats.get('copy', 0)}/3
- 图片成功: {stats.get('images', 0)}/6
- 视频成功: {'是' if stats.get('video') else '否'}

八、后续优化方向
1. 使用图生视频（wan2.7-i2v），以主图作为视频首帧提升一致性
2. 按目标平台（Shopee/Lazada/Amazon）定制关键词与 SEO 策略
3. 引入商品已有图片做图生图优化，保持品牌一致性
4. 对接真实货架页进行 A/B 测试，用点击率反哺生成策略
"""
    path = os.path.join(output_dir, "strategy_document.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    log.info(f"[策略] 已保存: {path}")


# ============================================================
# 主流程
# ============================================================

def main():
    args = parse_args()
    if args.version:
        print(VERSION)
        return 0

    if not args.prompt:
        log.error("缺少 --prompt 参数，退出")
        return 1
    if not API_KEY:
        log.error("环境变量 DASHSCOPE_API_KEY 未设置，退出")
        return 1

    log.info("=" * 60)
    log.info("SilkRoute Go Agent 启动")
    log.info(f"版本: {VERSION}")
    log.info(f"文本: {'/'.join(TEXT_MODELS)} | 图像: {IMAGE_MODEL} | 视频: {VIDEO_MODEL}")
    log.info(f"Prompt: {args.prompt[:300]}")
    log.info("=" * 60)

    input_dir, output_dir = extract_paths(args.prompt)
    os.makedirs(output_dir, exist_ok=True)
    log.info(f"输入目录: {input_dir}")
    log.info(f"输出目录: {output_dir}")

    log.info("[数据] 正在读取商品数据...")
    data = read_input_data(input_dir)
    if not data:
        log.error("未读取到任何商品数据文件，退出")
        return 1
    log.info(f"[数据] 共读取 {len(data)} 个数据源")

    insight = step_insight(data)
    stats = {
        "copy": step_descriptions(data, output_dir, insight),
        "images": step_images(data, output_dir, insight),
        "video": step_video(data, output_dir),
    }
    step_strategy(data, output_dir, insight, stats)

    log.info("=" * 60)
    log.info("全部任务完成！输出文件清单：")
    missing = []
    try:
        for fname in sorted(os.listdir(output_dir)):
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath):
                log.info(f"  {fname:35s} {os.path.getsize(fpath):>12,} bytes")
    except Exception:
        pass
    for fname in REQUIRED_FILES:
        p = os.path.join(output_dir, fname)
        if not (os.path.isfile(p) and os.path.getsize(p) > 0):
            missing.append(fname)
    log.info("=" * 60)
    if missing:
        log.error(f"以下必需产出缺失，判定失败: {missing}")
        return 2
    log.info("全部 11 项交付物齐备 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
