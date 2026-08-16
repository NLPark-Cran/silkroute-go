# SilkRoute Go · 丝路由

> 从一个 Skill 出发，让一件商品走上全球货架。
> 观猹 × 千问 AI 平台 · Go Live 创造营（2026-08-16 杭州）现场作品。

**SilkRoute Go** 是一个"一键出海 Agent"：选定一件商品与目标市场，Agent 自动完成
**市场洞察 → 多语言货架文案 → 视觉素材生成 → Go Live 上架**，
产出一条可公开访问、可扫码、能收集真实用户反馈的海外货架页。

- 🌏 线上地址：https://tabroad.hub.tt2.li
- 🧣 首个已上线货架：https://tabroad.hub.tt2.li/p/west-lake-mist-silk-scarf-90x90cm-6a-mulberry-si
- 🎤 闪电路演 Deck：https://tabroad.hub.tt2.li/pitch/

## 技术栈

| 层 | 选型 |
|---|---|
| 文本/洞察 | 千问AI平台 `qwen3.8-max`（DashScope OpenAI 兼容接口，fallback `qwen3.7-plus`） |
| 图像生成 | 千问AI平台 `qwen-image-3.0-pro`（multimodal-generation 同步接口） |
| 登录 | 观猹 OAuth2 Authorization Code（`https://watcha.cn/oauth`） |
| 后端 | FastAPI + Uvicorn（无容器） |
| 存储 | SQLite（WAL），生成图落盘 `app/static/generated/` |
| 部署 | 本机 nginx 反代 + certbot HTTPS，复用现有基础设施 |

## 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" httpx jinja2 python-multipart qrcode pillow
cp .env.example .env   # 填入 DASHSCOPE_API_KEY / WATCHA_CLIENT_ID / WATCHA_CLIENT_SECRET
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18100
```

## 目录

```
app/            FastAPI 应用（main/auth/db/llm/pipeline/seed）
app/templates/  落地页 / 工作台 / 英文货架页
pitch/          闪电路演网页 PPT（index.html 杂志风·森林墨 / swiss.html 瑞士风备份）
submission/     千问 Arena 提交版 Agent（agent.py 单文件入口，平台 --prompt 驱动）
baseline/       官方 Baseline 端点修复版（跑通验证用）
run_pipeline_once.py  免登录跑一次完整流水线（铺演示素材用）
```

## 千问 Arena 提交包

`submission/` 目录即提交 ZIP 内容（须打包为 ≤100MB ZIP）：
- 入口 `agent.py`，平台以 `--prompt "读取 <输入目录>...保存至 <输出目录>/"` 调用
- 产出官方交付清单全部 11 项：三语文案 txt ×3、main_image.png、detail_image_1~5.png、product_video.mp4、strategy_document.txt
- 相对 Baseline 的增强：修复图像/视频端点、修复带括号路径截断 bug、文本模型 qwen3.8-max 降级链、市场洞察前置步骤、图片逐张重试、关键产出缺失时非 0 退出

## 流水线实测

- 全流程 6 分 14 秒（含 3 张 1K 图生成）
- 图片成本约 ¥0.75（qwen-image-3.0-pro 1K 图 ¥0.25/张）
- 产出：英文标题/五点描述/SEO 关键词/品牌故事 + 主图/场景图/信息图 + 公开 URL + 二维码
