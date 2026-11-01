import { vi } from "vitest";

/**
 * jsdom ships no MediaRecorder / getUserMedia / enumerateDevices, so the test
 * environment installs the deterministic mocks defined here (see setup.ts).
 */

export interface BlobEventLike {
  data: Blob;
}

/** Registry of every MediaRecorder instance constructed during a test. */
export const mediaRecorderInstances: MockMediaRecorder[] = [];

export class MockMediaRecorder {
  static isTypeSupported = vi.fn((_type: string) => true);

  state: "inactive" | "recording" | "paused" = "inactive";
  mimeType: string;
  stream: unknown;

  ondataavailable: ((e: BlobEventLike) => void) | null = null;
  onstop: (() => void) | null = null;
  onstart: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;

  start = vi.fn((_timeslice?: number) => {
    this.state = "recording";
    this.onstart?.();
  });

  stop = vi.fn(() => {
    if (this.state === "inactive") return;
    this.state = "inactive";
    // Emit a final chunk, then signal stop (synchronously, like the spec flow).
    this.ondataavailable?.({
      data: new Blob(["mock-audio-bytes"], { type: this.mimeType }),
    });
    this.onstop?.();
  });

  pause = vi.fn(() => {
    this.state = "paused";
  });

  resume = vi.fn(() => {
    this.state = "recording";
  });

  requestData = vi.fn();

  addEventListener = vi.fn((type: string, cb: (e: unknown) => void) => {
    if (type === "dataavailable")
      this.ondataavailable = cb as (e: BlobEventLike) => void;
    else if (type === "stop") this.onstop = cb as () => void;
    else if (type === "start") this.onstart = cb as () => void;
    else if (type === "error") this.onerror = cb;
  });

  removeEventListener = vi.fn();
  dispatchEvent = vi.fn(() => true);

  constructor(stream: unknown, options?: { mimeType?: string }) {
    this.stream = stream;
    this.mimeType = options?.mimeType ?? "audio/webm";
    mediaRecorderInstances.push(this);
  }
}

/** Shared spies wired into navigator.mediaDevices. */
export const getUserMediaMock = vi.fn();
export const enumerateDevicesMock = vi.fn();

/** A fake MediaStream whose tracks can be stopped. */
export function makeFakeStream(): MediaStream {
  const track = { stop: vi.fn(), kind: "audio" };
  return {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream;
}

/** Build a DOMException-like error with a specific name. */
export function makeMediaError(name: string, message = name): Error {
  const err = new Error(message);
  err.name = name;
  return err;
}

export const AUDIO_INPUT_DEVICE = {
  deviceId: "default",
  kind: "audioinput",
  label: "Mock Microphone",
  groupId: "g1",
} as MediaDeviceInfo;

export const VIDEO_INPUT_DEVICE = {
  deviceId: "cam",
  kind: "videoinput",
  label: "Mock Camera",
  groupId: "g2",
} as MediaDeviceInfo;
