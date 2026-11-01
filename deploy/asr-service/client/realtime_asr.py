#!/usr/bin/env python3
"""
实时语音转文字 - Mac 客户端

通过麦克风采集音频，流式发送到 asr-host 服务端进行识别，实时显示结果。

Usage:
    python realtime_asr.py
    python realtime_asr.py --server 192.0.2.20 --port 19101
    python realtime_asr.py --list-devices    # 列出可用麦克风

依赖:
    pip install pyaudio websockets numpy
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pyaudio
import websockets

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1600    # 100ms per chunk at 16kHz
FORMAT = pyaudio.paInt16
BYTES_PER_SAMPLE = 2

# Silence threshold (RMS) for simple VAD on client side
SILENCE_THRESHOLD = 200


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------
def list_devices():
    """Print available audio input devices."""
    pa = pyaudio.PyAudio()
    print("\n可用音频输入设备:")
    print("-" * 60)
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            marker = " ← 默认" if i == pa.get_default_input_device_info().get("index") else ""
            print(f"  [{i}] {info['name']}{marker}")
            print(f"      采样率: {int(info['defaultSampleRate'])}Hz, 通道: {info['maxInputChannels']}")
    pa.terminate()


def create_stream(device_index: int | None = None):
    """Open a PyAudio input stream."""
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=CHUNK_SIZE,
    )
    return pa, stream


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
class Display:
    """Terminal display for real-time transcription."""

    def __init__(self):
        self.lines: list[str] = []
        self.current_partial = ""

    def show_partial(self, text: str):
        """Show interim result — overwrites current line."""
        if text != self.current_partial:
            self.current_partial = text
            sys.stdout.write(f"\r\033[K  💬 {text}")
            sys.stdout.flush()

    def commit_line(self, text: str):
        """Commit final result as a permanent line."""
        # Clear the partial line first
        sys.stdout.write("\r\033[K")
        if text.strip() and (not self.lines or text.strip() != self.lines[-1]):
            print(f"  ✓ {text}")
            sys.stdout.flush()
            self.lines.append(text.strip())
        self.current_partial = ""


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------
async def stream_mic(server: str, port: int, device_index: int | None):
    pa, stream = create_stream(device_index)
    display = Display()

    uri = f"ws://{server}:{port}"
    print(f"  Connecting to {uri} ...")

    # 绕过本地代理
    os.environ.setdefault("no_proxy", "")
    os.environ["no_proxy"] += f",{server}"

    try:
        async with websockets.connect(uri, max_size=10 * 1024 * 1024) as ws:
            # Wait for server ready
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            if data.get("type") == "status":
                print(f"  Server: {data.get('message')}")
            print(f"\n  🎤 开始说话... (Ctrl+C 退出)\n")

            async def sender():
                """Read mic and send audio chunks."""
                loop = asyncio.get_event_loop()
                while True:
                    # Run blocking pyaudio read in executor
                    pcm = await loop.run_in_executor(
                        None, stream.read, CHUNK_SIZE, False
                    )
                    await ws.send(pcm)
                    # Small yield to let receiver process
                    await asyncio.sleep(0)

            async def receiver():
                """Receive and display transcription results."""
                async for msg in ws:
                    if isinstance(msg, str):
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue

                        t = data.get("type")
                        if t == "partial":
                            text = data.get("text", "")
                            display.show_partial(text)
                        elif t == "final":
                            text = data.get("text", "")
                            display.commit_line(text)
                        elif t == "status":
                            pass  # Displayed once at start
                        elif t == "error":
                            print(f"\n  ⚠️  错误: {data.get('message')}")
                    # Ignore binary messages from server

            # Run sender and receiver concurrently
            sender_task = asyncio.create_task(sender())
            receiver_task = asyncio.create_task(receiver())

            done, pending = await asyncio.wait(
                [sender_task, receiver_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        print(f"\n  ❌ 无法连接到服务端: {e}")
        print(f"  请确认服务端已启动: ssh asr-host 'sudo systemctl status asr-streaming'")
    except KeyboardInterrupt:
        print("\n\n  已停止。")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="实时语音转文字客户端")
    parser.add_argument("--server", default="192.0.2.20", help="ASR 服务端地址")
    parser.add_argument("--port", type=int, default=19101, help="WebSocket 端口")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备索引")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    print("=" * 50)
    print("  实时语音转文字")
    print(f"  服务端: {args.server}:{args.port}")
    print("=" * 50)

    try:
        asyncio.run(stream_mic(args.server, args.port, args.device))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
