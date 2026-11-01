# ASR 语音识别服务接入文档

## 服务总览

| 服务 | 端口 | 协议 | systemd 单元 | 用途 |
|------|------|------|-------------|------|
| HTTP API | `19100` | REST（OpenAI 兼容） | `asr-service` | 离线文件转写、项目集成 |
| WebSocket 流式 | `19101` | WebSocket | `asr-streaming` | 实时麦克风语音转文字 |

| 项目 | 详情 |
|------|------|
| **服务器** | asr-host（192.0.2.20），Ubuntu 24.04 aarch64 |
| **GPU** | NVIDIA GB10，CUDA 13.0，PyTorch 2.14 |
| **模型** | SenseVoiceSmall（893MB），50+ 语言，GPU 170x 实时 |
| **框架** | FunASR 1.3.9 |
| **部署路径** | `/opt/asr-service/` |
| **开机自启** | ✅ 两个服务均通过 systemd 自动启动 |

---

## 一、HTTP API（离线文件转写）

### 1.1 健康检查

```bash
curl http://192.0.2.20:19100/health
# {"status": "ok", "device": "cuda:0", "models_loaded": ["sensevoice"]}
```

### 1.2 模型列表

```bash
curl http://192.0.2.20:19100/v1/models
# {"object": "list", "data": [{"id": "sensevoice"}, {"id": "fun-asr-nano"}, {"id": "paraformer"}]}
```

### 1.3 语音转写

**请求：**

```bash
curl http://192.0.2.20:19100/v1/audio/transcriptions \
  -F "file=@audio.wav" \
  -F "model=sensevoice" \
  -F "response_format=verbose_json"
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | binary | ✅ | 音频文件（wav / mp3 / m4a / webm） |
| `model` | string | ✅ | 当前加载：`sensevoice` |
| `response_format` | string | ❌ | `verbose_json`（含分段+时间戳）、`json`（仅文本）、`text` |
| `language` | string | ❌ | 如 `zh`。SenseVoice 自动检测，一般无需指定 |

**响应（verbose_json）：**

```json
{
  "task": "transcribe",
  "language": "zh",
  "duration": 125.4,
  "text": "我们来讨论一下第三季度的计划。好的，我有三个要点。",
  "segments": [
    {
      "id": 0, "seek": 0,
      "start": 0.42, "end": 3.86,
      "text": "我们来讨论一下第三季度的计划。",
      "tokens": [], "temperature": 1.0
    },
    {
      "id": 1, "seek": 0,
      "start": 4.21, "end": 7.18,
      "text": "好的，我有三个要点。",
      "tokens": [], "temperature": 1.0
    }
  ]
}
```

**字段映射到项目 `TranscriptSegment`：**

| API 字段 | 项目字段 | 说明 |
|----------|----------|------|
| `segments[].start` | `start` | 分段起始（秒） |
| `segments[].end` | `end` | 分段结束（秒） |
| `segments[].text` | `text` | 识别文本 |
| — | `speaker` | 当前模型不支持，默认 `None` |

### 1.4 模型选择

| 模型 | 场景 | 语言 | 速度 | 备注 |
|------|------|------|------|------|
| **sensevoice** | 通用会议转写 | 中/英/粤/日/韩等 50+ | GPU 170x 实时 | 当前加载 |
| **fun-asr-nano** | 高精度 + 方言/口音 | 31 语言 + 7 方言 | 中 | 800M 参数 |
| **paraformer** | 工业级中文 | 中文为主 | 中 | 含 VAD + 标点 |

### 1.5 项目接入

项目 `HttpAsrClient` 当前发送 multipart + `start`/`end` 分块参数，需适配 OpenAI 兼容接口：

```python
# app/clients/http.py — 适配方案

def _transcribe_chunk(self, audio_path, start, end):
    """改为发送完整文件到 OpenAI 兼容 API。"""
    timeout = httpx.Timeout(float(self.request_timeout_seconds))
    with open(audio_path, "rb") as fh:
        files = {"file": (os.path.basename(audio_path), fh, "audio/wav")}
        data = {"model": "sensevoice", "response_format": "verbose_json", "language": "zh"}
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.endpoint_url}/v1/audio/transcriptions",
                files=files, data=data,
            )
    response.raise_for_status()
    payload = response.json()
    return [
        {"start": s["start"], "end": s["end"], "text": s["text"]}
        for s in payload.get("segments", [])
    ]
```

`.env` 配置：

```env
ASR_IMPL=http
ASR_ENDPOINT_URL=http://192.0.2.20:19100
ASR_REQUEST_TIMEOUT_SECONDS=120
ASR_MAX_RETRIES=2
```

---

## 二、WebSocket 流式（实时麦克风转写）

### 2.1 通信协议

**客户端 → 服务端：** 二进制 PCM 音频帧（int16, 16kHz, mono，每帧 100ms）

**服务端 → 客户端（JSON）：**

```json
{"type": "status",  "message": "listening"}              // 连接就绪
{"type": "final",   "text": "你好世界", "duration": 2.3}  // 识别结果
{"type": "error",   "message": "..."}                     // 错误
```

**控制命令（客户端 → 服务端，文本帧）：**

```json
{"type": "reset"}   // 清空缓冲区，开始新一轮对话
```

服务端内置能量 VAD，自动检测说话/静音：说话停顿 ≥ 0.8 秒即视为断句，输出识别结果。

### 2.2 客户端使用

```bash
# 列出可用麦克风
./deploy/asr-service/.venv-client/bin/python3 \
  deploy/asr-service/client/realtime_asr.py --list-devices

# 开始实时转写
./deploy/asr-service/.venv-client/bin/python3 \
  deploy/asr-service/client/realtime_asr.py

# 指定设备和服务器
./deploy/asr-service/.venv-client/bin/python3 \
  deploy/asr-service/client/realtime_asr.py --device 1 --server 192.0.2.20
```

运行效果：

```
==================================================
  实时语音转文字
  服务端: 192.0.2.20:19101
==================================================
  Server: listening
  🎤 开始说话... (Ctrl+C 退出)

✓ 我们来讨论一下第三季度的计划
✓ 好的我有三个要点
✓ 第一个是关于市场扩张的方案
```

### 2.3 VAD 参数调优

编辑 `/opt/asr-service/scripts/streaming_server.py`：

```python
ENERGY_THRESHOLD = 300        # 声音能量阈值，越小越灵敏（50~500）
SILENCE_TIMEOUT_SEC = 0.8     # 静音多少秒触发断句
MIN_SPEECH_SEC = 0.3          # 最短有效语音，低于此值视为噪音
```

```bash
sudo systemctl restart asr-streaming
```

---

## 三、服务管理

```bash
# HTTP API 服务
sudo systemctl status asr-service
sudo systemctl restart asr-service
sudo journalctl -u asr-service -f

# WebSocket 流式服务
sudo systemctl status asr-streaming
sudo systemctl restart asr-streaming
sudo journalctl -u asr-streaming -f

# 快速管理脚本（仅用于调试）
/opt/asr-service/scripts/start.sh {start|stop|restart|status|test}
```

---

## 四、服务器目录结构

```
/opt/asr-service/
├── .venv/                              # Python 虚拟环境
├── models/modelscope/iic/
│   ├── SenseVoiceSmall/                # ASR 模型（897MB）
│   ├── cam++/                          # 说话人分离模型
│   ├── speech_fsmn_vad_*/              # 语音端点检测模型
│   └── punc_ct-transformer_*/          # 标点恢复模型（1.0GB）
├── logs/
│   ├── server.log                      # HTTP API 日志
│   └── streaming.log                   # 流式服务日志
├── scripts/
│   ├── start.sh                        # 管理脚本
│   └── streaming_server.py             # 流式服务端
├── asr-service.service                 # HTTP API systemd 配置
├── asr-streaming.service               # 流式服务 systemd 配置
├── test_asr.sh                         # 测试脚本
└── ASR-API.md                          # 本文档
```

> systemd 配置的正式副本位于 `/etc/systemd/system/asr-service.service` 和 `/etc/systemd/system/asr-streaming.service`。

---

## 五、常见问题

**Q: segments 为什么没有 speaker 字段？**
A: `sensevoice` 模型不输出说话人标签。如需说话人分离，需切换为 `paraformer` + `cam++` pipeline。如有需要可另行部署。

**Q: 音频格式有要求吗？**
A: 支持 wav / mp3 / m4a / webm。项目预处理后的 16kHz mono WAV 效果最佳。

**Q: 如何更新模型？**
A: 删除 `/opt/asr-service/models/modelscope/` 下对应模型目录，重启服务即自动拉取最新版本。

**Q: 两个服务会冲突吗？**
A: 不会。各自独立加载模型实例，GB10 有 121GB 统一内存，完全够用。

**Q: Mac 客户端提示权限不足？**
A: 系统设置 → 隐私与安全性 → 麦克风 → 允许终端访问。
