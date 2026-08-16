#!/usr/bin/env python3
"""
千问Arena Baseline Agent
========================
从商品数据自动生成跨境电商素材包：
  三语文案(英/韩/葡) + 主图 + 5张详情图 + 展示视频 + 策略文档

运行方式：
  python agent.py --prompt "读取 /input/ ... 保存至 /output/"
  python agent.py --version
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

# 从 agent.json 读取版本号
with open(AGENT_DIR / "agent.json", encoding="utf-8") as _f:
    VERSION = json.load(_f)["version"]

# 环境变量（由比赛沙箱注入）
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"
).rstrip("/")
OPENAI_BASE = os.environ.get(
    "OPENAI_BASE_URL", f"{DASHSCOPE_BASE}/compatible-mode/v1"
)

# ---- 模型选择（在这里切换即可） ----
TEXT_MODEL = "qwen3.5-plus"  # 备选 "qwen3.8-max"（更强但更慢）
IMAGE_MODEL = "qwen-image-3.0-pro"  # 备选 "qwen-image-3.0-pro"
VIDEO_MODEL = "happyhorse-1.1-t2v"  # 备选 "wan2.7-i2v-2026-04-25"（图生视频）

# ---- DashScope API 端点路径 ----
IMAGE_ENDPOINT = "/api/v1/services/aigc/multimodal-generation/generation"
VIDEO_ENDPOINT = "/api/v1/services/aigc/video-generation/video-synthesis"
TASK_ENDPOINT = "/api/v1/tasks"  # GET /api/v1/tasks/{task_id}

# ---- 异步任务轮询参数 ----
POLL_INTERVAL = 10  # 每次轮询间隔（秒）
POLL_TIMEOUT = 600  # 单任务最长等待（秒，10分钟）


# ============================================================
# 日志
# ============================================================

def setup_logging():
    """配置日志：同时写入文件和控制台"""
    log_dir = os.environ.get("AGENT_LOG_DIR", str(AGENT_DIR))
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("agent")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # 写日志文件
    fh = logging.FileHandler(
        os.path.join(log_dir, "agent.log"), encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 打印到控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = setup_logging()


# ============================================================
# 命令行参数 & 路径解析
# ============================================================

def parse_args():
    """解析 --prompt 和 --version 两个命令行参数"""
    parser = argparse.ArgumentParser(description="千问Arena Agent")
    parser.add_argument("--prompt", type=str, default="")
    parser.add_argument("--version", action="store_true")
    return parser.parse_args()


def extract_paths(prompt):
    """
    从 prompt 文本中提取输入目录和输出目录。
    例："读取 /home/user/ws/input/ ... 保存至 /home/user/ws/output/"
    """
    # 策略1：根据中文语义关键词定位
    inp = re.search(r"(?:读取|输入)\s*(/[\w/._-]+)", prompt)
    out = re.search(r"(?:保存至|保存到|输出)\s*(/[\w/._-]+)", prompt)
    if inp and out:
        return inp.group(1).rstrip("/") + "/", out.group(1).rstrip("/") + "/"

    # 策略2：提取所有类 Unix 路径，第一个视为输入、最后一个视为输出
    paths = re.findall(r"/[\w/._-]+/?", prompt)
    if len(paths) >= 2:
        return paths[0].rstrip("/") + "/", paths[-1].rstrip("/") + "/"

    # 兜底默认值
    log.warning("无法从 prompt 中解析路径，使用默认值")
    return "/home/user/ws/input/", "/home/user/ws/output/"


# ============================================================
# 读取商品数据
# ============================================================

def read_input_data(input_dir):
    """读取输入目录下的全部 JSON 数据文件，返回 {key: data} 字典"""
    result = {}
    base = Path(input_dir)

    # 已知文件（按比赛文档）
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

    # 扫描目录下其他 JSON（防止遗漏）
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
    """从商品数据中提取标题，找不到则返回通用描述"""
    basic = data.get("product_basic", {})
    if isinstance(basic, dict):
        for key in ("title", "name", "product_name", "product_title",
                     "商品名称", "标题"):
            if key in basic and basic[key]:
                return str(basic[key])
    # 在其它数据源中找
    for val in data.values():
        if isinstance(val, dict):
            for key in ("title", "name"):
                if key in val and val[key]:
                    return str(val[key])
    return "fashion clothing product"


# ============================================================
# 文本模型调用（千问，通过 OpenAI 兼容 API）
# ============================================================

def call_llm(system_prompt, user_prompt):
    """
    调用千问文本模型，返回生成的纯文本。
    使用 openai 库走 OPENAI_BASE_URL 兼容端点。
    """
    from openai import OpenAI  # 延迟导入，保证 --version 无需依赖

    client = OpenAI(api_key=API_KEY, base_url=OPENAI_BASE)
    resp = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=4096,
    )
    text = resp.choices[0].message.content or ""
    # 清理部分千问模型可能输出的 <think>...</think> 标签
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


# ============================================================
# DashScope 原生 API 工具函数
# ============================================================

def _ds_post(path, body):
    """向 DashScope 原生 API 发送 POST 请求（异步模式），返回 JSON"""
    url = f"{DASHSCOPE_BASE}{path}"
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _ds_get(path):
    """向 DashScope 发送 GET 请求，返回 JSON"""
    url = f"{DASHSCOPE_BASE}{path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {API_KEY}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def poll_task(task_id):
    """
    轮询 DashScope 异步任务，直到成功/失败/超时。
    成功时返回完整的任务结果 JSON。
    """
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        result = _ds_get(f"{TASK_ENDPOINT}/{task_id}")
        status = result.get("output", {}).get("task_status", "UNKNOWN")
        log.info(f"  轮询 {task_id[:16]}... 状态={status} ({elapsed}s)")

        if status == "SUCCEEDED":
            return result
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            msg = json.dumps(result, ensure_ascii=False)[:500]
            raise RuntimeError(f"任务异常终止: {msg}")

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    raise TimeoutError(f"任务 {task_id} 等待超过 {POLL_TIMEOUT}s")


def extract_result_urls(result):
    """从任务结果中提取产物 URL 列表（兼容多种返回格式）"""
    output = result.get("output", {})
    urls = []
    # 格式1：results 数组中的 url 字段
    for item in output.get("results", []):
        if isinstance(item, dict) and "url" in item:
            urls.append(item["url"])
    # 格式2：video_url 顶层字段
    if "video_url" in output:
        urls.append(output["video_url"])
    # 格式3：直接 url 字段
    if "url" in output:
        urls.append(output["url"])
    return urls


# ============================================================
# 文件下载
# ============================================================

def download_file(url, save_path):
    """从 URL 下载文件到本地路径"""
    if not url:
        log.warning(f"  跳过下载（URL 为空）: {save_path}")
        return False
    try:
        log.info(f"  下载 -> {os.path.basename(save_path)}")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            with open(save_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        size = os.path.getsize(save_path)
        log.info(f"  完成: {size:,} bytes")
        return True
    except Exception as e:
        log.error(f"  下载失败: {e}")
        return False


# ============================================================
# 步骤一：生成三语文案
# ============================================================

def step_descriptions(product_data, output_dir):
    """生成英文/韩文/葡萄牙文商品描述文案，保存到输出目录"""
    # 把全部商品数据序列化给 LLM 参考
    data_text = json.dumps(product_data, ensure_ascii=False, indent=2)
    if len(data_text) > 15000:
        data_text = data_text[:15000] + "\n... (数据已截断)"

    languages = [
        ("English", "英文", "product_description_en.txt"),
        ("Korean", "韩文", "product_description_ko.txt"),
        ("Portuguese", "葡萄牙文", "product_description_pt.txt"),
    ]

    for lang, label, filename in languages:
        log.info(f"[文案] 正在生成{label}文案...")
        try:
            system = (
                f"你是跨境电商文案专家。请用纯正的{lang}撰写商品描述。"
                "文案须包含：商品标题、SKU 信息（尺码/颜色等变体）、"
                "商品属性（材质/风格/场景）、平台名称与商品 ID/URL（如数据中有）、"
                "配套图片文件名(main_image, detail_image_1~5)和视频文件名"
                "(product_video.mp4)的简要介绍。"
            )
            user = (
                f"以下是商品的完整数据，请直接输出文案，不要加额外解释：\n\n"
                f"{data_text}"
            )
            text = call_llm(system, user)

            path = os.path.join(output_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log.info(f"  已保存: {filename} ({len(text)} 字符)")
        except Exception as e:
            log.error(f"  {label}文案生成失败: {e}")


# ============================================================
# 步骤二：生成图片（主图 + 5 张详情图）
# ============================================================

def step_images(product_data, output_dir):
    """调用图像模型生成 6 张商品图并下载"""
    title = get_product_title(product_data)

    # 6 个提示词：1 张主图 + 5 张详情图（不同角度/场景）
    specs = [
        ("main_image.png",
         f"{title}, professional e-commerce product photo, clean white "
         "background, studio lighting, high resolution, 8k, detailed"),
        ("detail_image_1.png",
         f"{title}, front view, full body, e-commerce photo, "
         "white background, professional"),
        ("detail_image_2.png",
         f"{title}, back view, full body, e-commerce photo, white background"),
        ("detail_image_3.png",
         f"{title}, close-up fabric texture detail, macro photography, "
         "high quality"),
        ("detail_image_4.png",
         f"{title}, lifestyle photo, model wearing, urban street style, "
         "natural lighting"),
        ("detail_image_5.png",
         f"{title}, flat lay arrangement with fashion accessories, "
         "top down view, aesthetic"),
    ]

    # multimodal-generation 为同步接口：直接返回图片 URL
    for filename, prompt in specs:
        log.info(f"[图片] 生成: {filename}")
        try:
            url = f"{DASHSCOPE_BASE}{IMAGE_ENDPOINT}"
            body = {
                "model": IMAGE_MODEL,
                "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                "parameters": {"size": "1024*1024", "prompt_extend": True},
            }
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            img_url = None
            for ch in resp.get("output", {}).get("choices", []):
                for part in ch.get("message", {}).get("content", []):
                    if "image" in part:
                        img_url = part["image"]
                        break
            if img_url:
                download_file(img_url, os.path.join(output_dir, filename))
                log.info(f"  已保存: {filename}")
            else:
                log.warning(f"  未获得图片 URL: {json.dumps(resp)[:300]}")
        except Exception as e:
            log.error(f"  {filename} 失败: {e}")


# ============================================================
# 步骤三：生成视频
# ============================================================

def step_video(product_data, output_dir):
    """调用视频模型生成商品展示视频并下载"""
    title = get_product_title(product_data)
    prompt = (
        f"{title}, product showcase video, rotating display, "
        "smooth camera movement, professional lighting, e-commerce style"
    )

    log.info("[视频] 提交视频生成任务...")
    try:
        resp = _ds_post(VIDEO_ENDPOINT, {
            "model": VIDEO_MODEL,
            "input": {"prompt": prompt},
            "parameters": {},
        })
        task_id = resp.get("output", {}).get("task_id")
        if not task_id:
            log.error(f"  视频任务提交异常: {resp}")
            return

        log.info(f"  任务已提交: {task_id[:16]}...")
        result = poll_task(task_id)
        urls = extract_result_urls(result)
        if urls:
            download_file(urls[0], os.path.join(output_dir, "product_video.mp4"))
        else:
            log.warning("  视频任务完成但未获得产物 URL")
    except Exception as e:
        log.error(f"  视频生成失败: {e}")


# ============================================================
# 步骤四：策略文档
# ============================================================

def step_strategy(product_data, output_dir):
    """生成并保存策略说明文档"""
    title = get_product_title(product_data)

    doc = f"""策略说明文档
{'=' * 50}

一、整体方案
本 Agent 采用全自动 AI 流水线，为商品「{title}」生成跨境电商素材包。
流程：读取商品数据 → 三语文案 → 主图+详情图 → 展示视频 → 策略文档。

二、模型选型
- 文案生成：{TEXT_MODEL}（通义千问文本大模型，多语言能力强）
- 图片生成：{IMAGE_MODEL}（万象图像生成模型，支持 1024x1024 高清电商图）
- 视频生成：{VIDEO_MODEL}（文生视频模型，生成商品展示短片）

三、文案策略
- 英文(en)：面向欧美市场，强调产品功能、品质与穿着场景
- 韩文(ko)：面向韩国市场，强调时尚感、穿搭建议与潮流元素
- 葡语(pt)：面向巴西/葡萄牙市场，突出性价比、舒适度与风格亮点

所有文案均包含：商品标题、SKU 变体信息、商品属性、平台名称/ID、
配套图片和视频文件名及简要说明。

四、视觉素材策略
- main_image.png    ：白底正面棚拍，突出商品整体外观（≥800x800px）
- detail_image_1.png：正面全身展示
- detail_image_2.png：背面全身展示
- detail_image_3.png：面料细节特写
- detail_image_4.png：模特穿搭生活场景
- detail_image_5.png：平铺搭配展示
- product_video.mp4 ：商品旋转展示短片

五、后续优化方向
1. 使用 image-to-video 模型(wan2.7-i2v)，以主图作为视频首帧，提升连贯性
2. 根据目标平台(Shopee/Lazada/Amazon)定制文案关键词与 SEO 策略
3. 引入商品已有图片做 image-to-image 优化，保持品牌一致性
4. A/B 测试不同图片风格对点击率的影响
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

    # --version：输出版本号后退出
    if args.version:
        print(VERSION)
        return

    # 参数校验
    if not args.prompt:
        log.error("缺少 --prompt 参数，退出")
        sys.exit(1)

    if not API_KEY:
        log.error("环境变量 DASHSCOPE_API_KEY 未设置，退出")
        sys.exit(1)

    log.info("=" * 60)
    log.info("千问Arena Agent 启动")
    log.info(f"版本: {VERSION}")
    log.info(f"文本模型: {TEXT_MODEL} | 图像模型: {IMAGE_MODEL} | 视频模型: {VIDEO_MODEL}")
    log.info(f"Prompt: {args.prompt[:300]}")
    log.info("=" * 60)

    # ---- 0. 解析输入/输出路径 ----
    input_dir, output_dir = extract_paths(args.prompt)
    os.makedirs(output_dir, exist_ok=True)
    log.info(f"输入目录: {input_dir}")
    log.info(f"输出目录: {output_dir}")

    # ---- 1. 读取商品数据 ----
    log.info("[数据] 正在读取商品数据...")
    data = read_input_data(input_dir)
    if not data:
        log.error("未读取到任何商品数据文件，退出")
        sys.exit(1)
    log.info(f"[数据] 共读取 {len(data)} 个数据源")

    # ---- 2. 生成三语文案 ----
    step_descriptions(data, output_dir)

    # ---- 3. 生成图片（主图 + 5 张详情图） ----
    step_images(data, output_dir)

    # ---- 4. 生成视频 ----
    step_video(data, output_dir)

    # ---- 5. 生成策略文档 ----
    step_strategy(data, output_dir)

    # ---- 汇总 ----
    log.info("=" * 60)
    log.info("全部任务完成！输出文件清单：")
    try:
        for fname in sorted(os.listdir(output_dir)):
            fpath = os.path.join(output_dir, fname)
            size = os.path.getsize(fpath)
            log.info(f"  {fname:35s} {size:>12,} bytes")
    except Exception:
        pass
    log.info("=" * 60)


if __name__ == "__main__":
    main()
