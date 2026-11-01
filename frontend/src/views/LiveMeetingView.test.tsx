import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { LiveMeetingView } from "./LiveMeetingView";
import {
  AUDIO_INPUT_DEVICE,
  enumerateDevicesMock,
  getUserMediaMock,
  makeFakeStream,
  makeMediaError,
  mediaRecorderInstances,
} from "../test/mediaMocks";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readyState = 0;
  binaryType = "";
  sent: Array<unknown> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string, public protocols: string[]) {
    MockWebSocket.instances.push(this);
  }
  send(d: unknown): void {
    this.sent.push(d);
  }
  close(): void {
    this.readyState = 3;
  }
  simulateOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  simulateMessage(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent);
  }
}

function mockStartLiveFetch(): void {
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 201,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => ({
      id: "m-live-1",
      status: "live",
      title: "会议-x",
      send_interval: 1,
      reconnect_interval: 3,
    }),
  })) as unknown as typeof fetch;
}

function renderView() {
  return render(
    <MemoryRouter initialEntries={["/live"]}>
      <LiveMeetingView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.setItem("mm_token", "tok-123");
  MockWebSocket.instances.length = 0;
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("LiveMeetingView device/permission gating (Req 2.4/2.5)", () => {
  it("shows a permission message and does not start when mic is denied", async () => {
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockRejectedValue(makeMediaError("NotAllowedError"));
    renderView();
    await userEvent.click(screen.getByRole("button", { name: "开始会议" }));
    expect(await screen.findByText("需要麦克风权限才能开始会议")).toBeInTheDocument();
    expect(MockWebSocket.instances).toHaveLength(0); // no Streaming_Connection
  });

  it("shows a no-device message when there is no audio input", async () => {
    enumerateDevicesMock.mockResolvedValue([]);
    renderView();
    await userEvent.click(screen.getByRole("button", { name: "开始会议" }));
    expect(await screen.findByText("未找到音频输入设备")).toBeInTheDocument();
    expect(MockWebSocket.instances).toHaveLength(0);
  });
});

describe("LiveMeetingView live session", () => {
  it("starts the recorder only after the socket opens, shows the preview, and relays", async () => {
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockResolvedValue(makeFakeStream());
    mockStartLiveFetch();

    renderView();
    await userEvent.click(screen.getByRole("button", { name: "开始会议" }));

    // The socket is created, but the MediaRecorder must NOT start until open.
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    const ws = MockWebSocket.instances[0];
    expect(ws.protocols).toEqual(["mm.live.v1", "tok-123"]);
    expect(mediaRecorderInstances).toHaveLength(0); // FIX 6: not started yet

    await act(async () => {
      ws.simulateOpen();
    });
    // Now the recorder is running and the live UI is shown.
    await waitFor(() => expect(mediaRecorderInstances).toHaveLength(1));
    expect(screen.getByTestId("preview-notice")).toHaveTextContent("预览，非最终记录");
    expect(screen.getByRole("button", { name: "结束会议" })).toBeInTheDocument();

    // A transcript frame renders into the preview.
    await act(async () => {
      ws.simulateMessage({
        type: "transcript",
        result: {
          segments: [
            { start: 0, end: 1, text: "你好世界", speaker: "A", is_final: true },
          ],
        },
      });
    });
    expect(screen.getByTestId("live-preview")).toHaveTextContent("A：你好世界");

    // An asr_unavailable frame surfaces the degradation indicator.
    await act(async () => {
      ws.simulateMessage({ type: "asr_unavailable" });
    });
    expect(screen.getByTestId("asr-unavailable")).toBeInTheDocument();
  });
});

describe("LiveMeetingView recorder failure + keepalive", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces an error and returns to idle when MediaRecorder construction fails (Safari webm case)", async () => {
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockResolvedValue(makeFakeStream());
    mockStartLiveFetch();
    // Safari-like environment: no candidate mime is supported and the
    // constructor throws NotSupportedError.
    class ThrowingRecorder {
      static isTypeSupported = vi.fn(() => false);
      constructor() {
        throw makeMediaError("NotSupportedError", "unsupported mime type");
      }
    }
    vi.stubGlobal("MediaRecorder", ThrowingRecorder);

    renderView();
    await userEvent.click(screen.getByRole("button", { name: "开始会议" }));
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    await act(async () => {
      MockWebSocket.instances[0].simulateOpen();
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/无法启动录音/);
    // Back to idle so the user can retry.
    expect(screen.getByRole("button", { name: "开始会议" })).toBeInTheDocument();
  });

  it("sends a keepalive control frame every 15s while live", async () => {
    vi.useFakeTimers();
    enumerateDevicesMock.mockResolvedValue([AUDIO_INPUT_DEVICE]);
    getUserMediaMock.mockResolvedValue(makeFakeStream());
    mockStartLiveFetch();
    renderView();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "开始会议" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.simulateOpen();
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    vi.useRealTimers();
    const controls = ws.sent
      .filter((s): s is string => typeof s === "string")
      .map((s) => JSON.parse(s) as { type: string });
    expect(controls.filter((m) => m.type === "keepalive").length).toBeGreaterThanOrEqual(1);
  });
});
