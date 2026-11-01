# 部署 / 更新手册（192.0.2.10）

本手册记录如何把本仓库的新版本部署到内网 **`192.0.2.10`** 上的 `meeting-minutes` 服务。
适用于日常的「替换成新版本」更新。按顺序照做即可。

> ASR / 流式 ASR / 本地 LLM 仍运行在 **DGX Spark（`192.0.2.20`，SSH 别名 `asr-host`）**；
> 本手册只描述会议纪要应用栈（Docker Compose）在 `.10` 上的部署。

> 一句话流程：本地提交 → 备份线上 DB → `tar` 同步代码（务必带 `COPYFILE_DISABLE=1`）→
> （如改了 nginx）合并进线上 TLS 配置 → `docker compose build` → `docker compose up -d`
> → **重载 nginx** → 验证。

---

## 0. 部署目标与环境

| 项 | 值 |
|---|---|
| 主机 | 内网 `192.0.2.10`（AlmaLinux x86_64），arch `linux/amd64` |
| SSH | `deploy@192.0.2.10:22`，密码 `REDACTED`（该账号可 `sudo`，同样用 `REDACTED`） |
| 快速连接 | `sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10` |
| 部署目录 | `/home/deploy/meeting-minutes`（**不是 git 仓库**，靠 `tar`/`scp` 同步） |
| 编排 | Docker Compose，项目名 `meeting-minutes`（普通用户需 `sudo` 调用 `docker compose`） |
| 服务 | `api`、`worker`（同一镜像）、`frontend`、`redis`、`nginx` |
| 数据卷 | `meeting-minutes_appdata`（SQLite `app.db` + 音频）、`meeting-minutes_redis-data` |
| 对外端口 | nginx `80`（301 跳 https）、`443`（自签证书 TLS，CN/SAN = `192.0.2.10`） |
| 构建平台 | 默认 `DOCKER_PLATFORM=linux/amd64`（`.env` 或 compose 环境变量） |

> ⚠️ **同一台主机上还跑着其它服务**。
> 所有 docker 操作都必须**限定在 `meeting-minutes` compose 项目内**，
> **绝不**执行 `docker system prune` / `docker image prune -a` 等全局清理命令。
>
> ⚠️ **SSH 账号是普通用户 `deploy`，不是 root**。所有 `docker compose` 命令都需要
> `sudo`（`echo REDACTED | sudo -S docker compose ...`），且**不要在一条 ssh 里串联两次
> `sudo`**（第二次会因密码管道已关闭而 `Permission denied`）——需要多次提权时分开成多次 ssh，或用
> `echo REDACTED | sudo -S bash -c '多条命令'` 一次性执行。

---

## 1. 线上「定制文件」——只保留、绝不覆盖

线上有几处和仓库**不一样**的本地定制，更新时必须原样保留，**不要**用仓库版本覆盖：

| 文件 | 作用 | 与仓库的差异 |
|---|---|---|
| `.env` | 线上密钥 / 模型端点配置 | 仓库不含（已 gitignore） |
| `docker-compose.override.yml` | 给 nginx 加 `443` 端口 + 证书挂载 | 仓库不含 |
| `deploy/nginx/nginx.conf` | **带 TLS** 的反代配置（HTTP→HTTPS 跳转、证书、WS 代理） | 仓库是**纯 HTTP** 版，结构完全不同 |
| `deploy/nginx/certs/` | 自签证书 `cert.pem` / `key.pem`（CN/SAN = `192.0.2.10`） | 仓库不含 |

> 因此：**改了 nginx 配置时，要把改动「合并」进线上这份 TLS 配置，而不是整份替换**（见第 4 步）。

---

## 2. 备份线上数据（更新前必做）

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes && TS=$(date +%Y%m%d-%H%M%S) \
  && mkdir -p ~/meeting-minutes-backups \
  && echo REDACTED | sudo -S docker compose cp api:/data/db ~/meeting-minutes-backups/db-$TS \
  && ls -la ~/meeting-minutes-backups/db-$TS'
```

备份会拷出 `app.db` + `app.db-wal` + `app.db-shm` 到 `~/meeting-minutes-backups/db-<时间戳>`。
改 nginx 前同样建议备份原配置（命令见第 4 步）。

---

## 3. 同步代码（本地 → 192.0.2.10）

部署目录不是 git 仓库，用 `tar` over ssh 增量覆盖（只新增/更新文件，不删除、不碰第 1 节的定制文件）。

> ❗**最重要的一条**：必须设 `COPYFILE_DISABLE=1`。
> macOS 的 `tar` 默认会把 `._*`（AppleDouble 资源叉/xattr）一起打进归档，
> 在 Linux 解出后会变成 `migrations/versions/._0002_xxx.py` 这类**含 null 字节的二进制文件**，
> 而 alembic 会扫描 `versions/` 下所有 `*.py`（包括点文件），加载时直接崩
> `SyntaxError: source code string cannot contain null bytes`，导致 api 启动失败。

在**本机仓库根目录**执行：

```bash
# 后端（排除 venv / 缓存 / egg-info）
COPYFILE_DISABLE=1 tar czf - -C backend \
  --exclude='./.venv' --exclude='./.hypothesis' --exclude='./.pytest_cache' \
  --exclude='./.localrun' --exclude='./meeting_minutes_service.egg-info' \
  --exclude='.DS_Store' . \
  | sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'tar xzf - -C /home/deploy/meeting-minutes/backend'

# 前端（排除 node_modules / dist）
COPYFILE_DISABLE=1 tar czf - -C frontend \
  --exclude='./node_modules' --exclude='./dist' --exclude='.DS_Store' . \
  | sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'tar xzf - -C /home/deploy/meeting-minutes/frontend'
```

**保险：同步后清一遍 AppleDouble + 旧字节码**（即使忘了 `COPYFILE_DISABLE` 也能补救）：

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes \
  && find backend frontend -name "._*" -delete \
  && find backend -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; echo cleaned'
```

> 注意：macOS 自带的 `rsync` 其实是 `openrsync`，对 `--exclude` 兼容性差，**别用 rsync**，用上面的 `tar`。
> Linux 端 `tar` 解包时打印的 `LIBARCHIVE.xattr.com.apple.provenance` 警告无害，可忽略。

---

## 4. （仅当改了 nginx）把改动合并进线上 TLS 配置

线上 `deploy/nginx/nginx.conf` 是带 TLS 的版本，不能用仓库版覆盖。先备份再编辑：

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'echo REDACTED | sudo -S cp /home/deploy/meeting-minutes/deploy/nginx/nginx.conf \
  ~/meeting-minutes-backups/nginx.conf.bak-$(date +%Y%m%d-%H%M%S)'
```

实时会议（WebSocket）需要在该文件里有这几处（已在当前线上配置就位，仅供参考/校对）：

- `http` 作用域顶部（conf.d 被 include 在 `http{}` 内，可直接写）：
  ```nginx
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  ```
- `443` server 的 `location /api/` 内：
  ```nginx
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection $connection_upgrade;
  proxy_read_timeout 3600s;   # 须远大于 live_inactivity_timeout_seconds(默认300)
  proxy_send_timeout 3600s;
  ```

编辑方式：在本机改好完整文件后 `scp` 覆盖线上这一份（注意保留 TLS/证书段落）：

```bash
sshpass -p 'REDACTED' scp -o StrictHostKeyChecking=no deploy/nginx/nginx.conf deploy@192.0.2.10:/home/deploy/meeting-minutes/deploy/nginx/nginx.conf
```

---

## 5. 构建镜像

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes && echo REDACTED | sudo -S docker compose build'
```

- 只会构建本项目的 `api`/`worker`（同镜像）/`frontend`，不影响其它服务。
- **首次或缓存失效时，`api` 镜像安装 ffmpeg 的 apt 层可能较慢（外网慢）**；之后会命中缓存。
- 到 PyPI/apt 镜像偶发超时（`Read timed out`）。**遇到网络超时直接重跑 `docker compose build` 即可**，已下载的层会复用。
- 因为 ssh 长时间无输出会被判超时，建议**后台跑 + 轮询日志**：
  ```bash
  sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes && \
    echo REDACTED | sudo -S setsid bash -c "docker compose build > /tmp/mm-build.log 2>&1" </dev/null >/dev/null 2>&1 & echo launched'
  # 之后查看进度：
  sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'echo REDACTED | sudo -S tail -20 /tmp/mm-build.log; echo REDACTED | sudo -S pgrep -f "docker compose build" >/dev/null && echo RUNNING || echo DONE'
  ```
  > ⚠️ **判断构建是否结束别只看 `pgrep`**：`pgrep -f "docker compose build"` 会把 `pgrep`
  > 命令自身也匹配上（命令行含该串），造成「永远 RUNNING」的误判。日志输出出现
  > `api Built / frontend Built / worker Built` 后，再确认 `/tmp/mm-build.log` 大小不再
  > 变化，即为真正完成。

---

## 6. 滚动更新 + 重载 nginx

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes && echo REDACTED | sudo -S docker compose up -d'
```

- 只会重建镜像有变化的服务（`api`/`worker`/`frontend`），`redis` 不动。
- **数据库迁移是自动的**：`api` 容器启动时 entrypoint 会跑 `alembic upgrade head`。
  迁移均为**纯加列/加表**的向后兼容改动，**不动既有数据**，命名卷里的 `app.db` 原样保留。
  当前线上 head 为 `0003_admin_console`；每次部署后用第 7 步的 `alembic current` 确认版本。
  无需任何手动迁移步骤。
- **api 重建后 IP 会变，必须重载 nginx**，否则 nginx 仍指向旧 api 容器 → 返回 `502 Bad Gateway`：

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes \
  && echo REDACTED | sudo -S bash -c "docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload"'
```
> 用 `sudo bash -c "..."` 把两条命令放在**一次提权**里执行，避免同一 ssh 会话里第二次
> `sudo` 因密码管道已关闭而 `Permission denied`。

> 改了 `deploy/nginx/nginx.conf` 时也靠这步生效（配置是 bind-mount，`reload` 会重新读取）。
> 若 `nginx -t` 报错，先修配置再 reload；线上原配置已备份在 `~/meeting-minutes-backups/`。

---

## 7. 验证

```bash
sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes \
  && echo REDACTED | sudo -S bash -c "
    echo \"=== 容器状态 ===\" && docker compose ps --format \"table {{.Name}}\t{{.Status}}\" \
    && echo \"=== 迁移版本 ===\" && docker compose exec -T api python -m alembic current \
    && echo \"=== HTTPS 健康检查 ===\" && curl -sk https://localhost/api/health && echo \
    && echo \"=== live 路由(应 401) ===\" \
    && curl -sk -o /dev/null -w \"%{http_code}\n\" -X POST https://localhost/api/meetings/live \
    && echo \"=== 注册路由(默认应 403) ===\" \
    && curl -sk -o /dev/null -w \"%{http_code}\n\" -X POST https://localhost/api/auth/register -H \"Content-Type: application/json\" -d \"{\\\"username\\\":\\\"x\\\",\\\"password\\\":\\\"y\\\"}\" \
    && echo \"=== HTTP→HTTPS(应 301) ===\" \
    && curl -s -o /dev/null -w \"%{http_code}\n\" http://localhost/
  "'
```

期望结果：

- 所有容器 `Up`，`api` 为 `(healthy)`。
- `alembic current` = `0003_admin_console (head)`。
- 健康检查：`{"status":"ready","dependencies":{"database":"up","redis":"up"}}`。
- `POST /api/meetings/live` → `401`（路由已注册、需登录；若 `404` 说明后端没更新，`502` 说明 nginx 没 reload）。
- `POST /api/auth/register` → `403`（注册端点已注册、默认关闭；若 `404` 说明后端没更新）。
- `http://` → `301`。
- 浏览器打开 `https://192.0.2.10/`（自签证书需手动信任），列表页能看到「新建会议 / 开始实时会议」入口。

---

## 8. 回滚

- **代码/镜像**：重新同步上一版代码并 `docker compose build && up -d`（建议每次部署前用 `git tag`/分支标记可回滚点）。
- **数据库**：用第 2 步的备份恢复：
  ```bash
  sshpass -p 'REDACTED' ssh -o StrictHostKeyChecking=no deploy@192.0.2.10 'cd /home/deploy/meeting-minutes \
    && echo REDACTED | sudo -S bash -c "docker compose stop api worker \
    && docker compose cp ~/meeting-minutes-backups/db-<时间戳>/app.db api:/data/db/app.db \
    && docker compose start api worker"'
  ```
  （`0003` 是纯加列/加表的向后兼容迁移，旧代码也能读新库；一般无需降级。如确需降级：`docker compose exec api python -m alembic downgrade -1`。）
- **nginx**：把 `~/meeting-minutes-backups/nginx.conf.bak-*` 拷回去再 `nginx -s reload`。

---

## 9. 切换到真实 ASR / LLM 模型（无需改代码、无需重建镜像）

编辑线上 `.env` 后 `docker compose up -d`（重启读取新配置）即可：

```ini
ASR_IMPL=http
ASR_ENDPOINT_URL=http://192.0.2.20:19100
ASR_MODEL_NAME=sensevoice

LLM_IMPL=http
LLM_ENDPOINT_URL=https://api.deepseek.com      # 或本地 vLLM/Ollama
LLM_MODEL_NAME=deepseek-v4-flash
LLM_API_KEY=...                                 # 云端供应商需要；本地可省
```

实时转写额外开关（ASR 仍在 `192.0.2.20`）：

```ini
STREAMING_ASR_IMPL=websocket
STREAMING_ASR_WS_URL=ws://192.0.2.20:19101
```

---

## 10. 实时会议路径的部署约束（重要）

实时流式路径的并发协调（每会议的 append 锁、连接抢占信号、缓存的 WebM 头、ASR 队列）都是
**进程内内存状态**，只在**单进程**内保证「一个录音同一时刻只有一个写入者」。
因此 `api` **必须单进程 / 单副本**运行：

- `docker-compose.yml` 里 `api` 的命令保持 `uvicorn app.main:app`，**不要加 `--workers`**；
- **不要**换成多 worker 的 gunicorn；
- **不要**对 `api` 扩副本（`deploy: replicas > 1` 或 `--scale api=N`）。

DB 的部分唯一索引只保证「每用户同时一个 live 会话」（跨进程），不保证「每录音单写入者」。
将来要横向扩展，需先把进程内 append 锁换成跨进程锁（Redis / DB 咨询锁）+ 共享抢占信号。

---

## 附：本次踩坑速记

1. **macOS `tar` 的 `._*` 文件**让 alembic 崩 null bytes → 用 `COPYFILE_DISABLE=1`，并在同步后 `find -name "._*" -delete`。
2. **macOS `rsync`（openrsync）** 不兼容 `--exclude` → 改用 `tar` over ssh。
3. **api 重建后不 reload nginx** → `502`（nginx 缓存了旧上游 IP）。
4. **构建机外网慢** → `docker compose build` 的 apt/pip 偶发超时，重跑即可。
5. **同主机有其它服务** → 只用项目级 `docker compose`，禁止全局 `prune`。
