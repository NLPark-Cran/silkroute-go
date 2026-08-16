# AGENTS.md — SilkRoute Go（testabroad）

## 项目背景

观猹 × 千问 AI 平台 Go Live 创造营（2026-08-16）现场作品：一键出海 Agent。
线上：https://tabroad.hub.tt2.li ｜ 仓库：https://github.com/NLPark-Cran/silkroute-go

## 技术约定

- **后端**：FastAPI（`app/main.py`），Starlette 1.6 新版签名 `TemplateResponse(request, name, context)` —— 不要用旧的位置参数写法。
- **模型**：走 DashScope（`https://dashscope.aliyuncs.com`），key 用千问AI平台 `sk-ws-*`。
  - 文本：`qwen3.8-max`，失败时 fallback `qwen3.7-plus`；结构化输出用 `response_format={"type":"json_object"}`。
  - 图像：`qwen-image-3.0-pro`，同步接口 `/api/v1/services/aigc/multimodal-generation/generation`，RPM=5，图床 URL 需及时下载落盘。
- **观猹 OAuth**：测试 client_id `1p9Mcr+CNLPAMFC0`（含 `+`，URL 传参必须 `quote` 编码）。回调 `{BASE_URL}/auth/callback`。
- **存储**：SQLite stdlib（`app/db.py`），勿引入 ORM 保持轻量。
- **部署**：不用容器。uvicorn 跑在 `127.0.0.1:18100`，nginx 反代 `tabroad.hub.tt2.li`，certbot 管证书。重启：`kill <pid>` 后 `setsid .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18100 &`。
- **密钥**：`.env` 存密钥且不入库；`.gitignore` 已排除 `.env`、`data/`、`app/static/generated/`、`task_data/`。
- **Skill**：千问技能市场 CLI `qianwen`（>=1.4.0），技能装在 `/root/.agents/skills/`；路演 PPT 用 guizang-ppt-skill 瑞士风（`/root/.claude/skills/guizang-ppt-skill/`）。
- **文案**：中文文案遵循 Humanizer-zh 原则，避免 AI 腔。

## 演示数据

- 首个货架 slug：`west-lake-mist-silk-scarf-90x90cm-6a-mulberry-si`
- 商品源数据：`task_data/Task_Data/Data_for_Users(2)/product_info/*.json`（不进仓库）
