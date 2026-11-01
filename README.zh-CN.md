# MeetingMint 🍃

**每一场会议,都产出新鲜的纪要。**

[English](README.md) | 简体中文

会议录音上传进去,一份结构化、可编辑、可导出的会议纪要生成出来。MeetingMint
是一个可自托管的会议纪要服务,支持批量转写与实时转写双模式,ASR / LLM
端点可插拔,全部数据都留在你自己的网络里。

## 功能特性

- **📤 批量处理** — 上传最大 500 MB 的会议音频;异步 Celery 流水线依次执行
  预处理 → 转写 → 生成,产出结构化、可编辑、可导出的会议纪要。
- **🔴 实时会议** — 基于 WebSocket 流式 ASR 的实时转写,带实时说话人标签,
  会议中途断线后自动恢复。
- **🔌 模型可插拔** — ASR 与 LLM 均为外部 OpenAI 兼容 HTTP 服务(SenseVoice /
  FunASR、DeepSeek、vLLM、Ollama 等)。内置确定性的 `mock` 实现,完全不部署
  任何模型也能运行和测试整个系统。
- **👥 账号与管理后台** — 自助注册、按用户隔离的会议工作区,以及覆盖用户
  管理、运行时配置、任务监控和审计日志的管理控制台。
- **🐳 自托管** — 一条 `docker compose up` 即可启动,前置 Nginx(TLS);
  音频、转写文本和日志永不离开你的网络。

## 架构

```
frontend (Vite + TypeScript)
   │
nginx (TLS, port 80/443)
   │
api (FastAPI) ── redis ── worker (Celery) ──► ASR service (OpenAI-compatible HTTP)
   │                                            LLM service (OpenAI-compatible HTTP)
postgres + audio/file storage
```

ASR / LLM 集成是配置背后的客户端抽象(`ASR_IMPL` / `LLM_IMPL` =
`mock` | `http`),因此后端在没有任何模型服务的情况下也可以完整开发和测试。

## 快速开始

```bash
cp .env.example .env      # 默认配置下全部以 mock 模式运行
docker compose up -d --build
```

打开 <http://localhost/>,使用 `.env` 中的引导管理员账号登录
(`BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` —— 真实部署前请务必
修改默认值),然后创建你的第一场会议。

当 `ASR_IMPL=mock` / `LLM_IMPL=mock` 时,整条流水线会用确定性的替身实现端到端
跑通 —— 适合在不部署任何模型的情况下体验 UI 和 API。

## 配置

将 `.env.example` 复制为 `.env` 并按需填写。关键配置项:

| 变量 | 用途 |
|---|---|
| `ASR_IMPL` / `LLM_IMPL` | `mock`(无需模型)或 `http`(真实端点,启动时校验) |
| `ASR_ENDPOINT_URL` | OpenAI 兼容的转写端点,例如 SenseVoice/FunASR 服务 |
| `LLM_ENDPOINT_URL` / `LLM_MODEL_NAME` / `LLM_API_KEY` | 任意 OpenAI 兼容的聊天端点(DeepSeek、本地 vLLM / Ollama 等) |
| `STREAMING_ASR_WS_URL` | 实时会议使用的 WebSocket 流式 ASR 端点 |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | 初始管理员账号,启动时做哈希 |
| `MAX_UPLOAD_BYTES` | 上传大小上限(默认 500 MB) |

完整带注释的列表见 `.env.example`。

## 本地开发

**后端**(Python 3.12、FastAPI、Celery、SQLAlchemy/Alembic):

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

**前端**(Vite + TypeScript + React):

```bash
cd frontend
npm ci
npm run dev        # 或:npm run build / typecheck / test
```

## 文档

- [`DEPLOY.md`](DEPLOY.md) — 生产环境部署 / 更新手册(Docker Compose + Nginx)
- [`backend/README.md`](backend/README.md) — 后端结构与开发指南
- [`deploy/asr-service/ASR-API.md`](deploy/asr-service/ASR-API.md) — ASR / 流式 ASR 服务 API

## 许可证

[MIT](LICENSE)
