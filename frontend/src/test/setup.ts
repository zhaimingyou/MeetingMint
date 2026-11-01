import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import {
  MockMediaRecorder,
  enumerateDevicesMock,
  getUserMediaMock,
  mediaRecorderInstances,
} from "./mediaMocks";

// Node 26 may expose an unusable global localStorage getter to jsdom test
// workers. Replace it with an in-memory store for every test worker; this file
// is never included in the production bundle.
function makeMemoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(String(key), String(value));
    },
  };
}

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: makeMemoryStorage(),
});

// --- MediaRecorder ---
(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder =
  MockMediaRecorder;

// --- navigator.mediaDevices ---
try {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    writable: true,
    value: {
      getUserMedia: getUserMediaMock,
      enumerateDevices: enumerateDevicesMock,
    },
  });
} catch {
  (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
    getUserMedia: getUserMediaMock,
    enumerateDevices: enumerateDevicesMock,
  };
}

// --- URL.createObjectURL / revokeObjectURL (absent in jsdom) ---
if (typeof URL.createObjectURL !== "function") {
  (URL as unknown as { createObjectURL: () => string }).createObjectURL =
    vi.fn(() => "blob:mock");
}
if (typeof URL.revokeObjectURL !== "function") {
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
}

// --- matchMedia (absent in jsdom) ---
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // Drop any per-test implementations on the shared device spies.
  getUserMediaMock.mockReset();
  enumerateDevicesMock.mockReset();
  // Re-arm the default "everything supported" behavior cleared above.
  MockMediaRecorder.isTypeSupported.mockReturnValue(true);
  mediaRecorderInstances.length = 0;
  localStorage.clear();
});
