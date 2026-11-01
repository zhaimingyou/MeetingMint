import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fc from "fast-check";
import {
  LiveSocket,
  applyResult,
  emptyPreview,
  frameIncrement,
  previewSegments,
} from "./live";
import type { LiveTranscriptResult } from "./types";

// --- Property 42: preview reduction invariants -----------------------------

const segArb = (isFinal: boolean) =>
  fc.record({
    start: fc.double({ min: 0, max: 1000, noNaN: true }),
    end: fc.double({ min: 0, max: 1000, noNaN: true }),
    text: fc.string(),
    speaker: fc.option(fc.string(), { nil: null }),
    is_final: fc.constant(isFinal),
  });

const resultArb: fc.Arbitrary<LiveTranscriptResult> = fc.record({
  segments: fc.tuple(
    fc.array(segArb(true), { maxLength: 4 }),
    fc.array(segArb(false), { maxLength: 2 }),
  ).map(([finals, partials]) => [...finals, ...partials]),
});

// Feature: realtime-meeting-transcription, Property 42: The transcript preview orders finalized segments ascending, positions partials after them, and replaces superseded partials.
// Validates: Requirements 3.4, 3.5, 3.7
describe("Property 42: live transcript preview reduction", () => {
  it("keeps finalized ascending, partials after, and replaces superseded partials", () => {
    fc.assert(
      fc.property(fc.array(resultArb, { minLength: 1, maxLength: 20 }), (results) => {
        let state = emptyPreview();
        let prevFinalStarts = new Set<number>();
        for (const result of results) {
          const prevPartials = state.partials;
          state = applyResult(state, result);

          // Finalized sorted ascending by start.
          for (let i = 1; i < state.finalized.length; i++) {
            expect(state.finalized[i].start).toBeGreaterThanOrEqual(
              state.finalized[i - 1].start,
            );
            expect(state.finalized[i].is_final).toBe(true);
          }
          // Finalized set is monotonic (never loses a start it once had).
          for (const s of prevFinalStarts) {
            expect(state.finalized.some((f) => f.start === s)).toBe(true);
          }
          prevFinalStarts = new Set(state.finalized.map((f) => f.start));

          // previewSegments places all finalized before any partial.
          const flat = previewSegments(state);
          const firstPartial = flat.findIndex((s) => !s.is_final);
          if (firstPartial !== -1) {
            expect(flat.slice(firstPartial).every((s) => !s.is_final)).toBe(true);
          }

          // Superseded partials never remain: if this result carried any
          // segment, the displayed partials are exactly this result's partials.
          if (result.segments.length > 0) {
            const resultPartials = result.segments.filter((s) => !s.is_final);
            expect(state.partials).toEqual(resultPartials);
            // None of the prior partial objects linger.
            for (const old of prevPartials) {
              expect(state.partials.includes(old)).toBe(
                resultPartials.includes(old),
              );
            }
          }
        }
      }),
      { numRuns: 100 },
    );
  });
});

// --- LiveSocket framing + resume replay ------------------------------------

class FakeWebSocket {
  readyState = 1; // OPEN
  binaryType = "";
  sent: Array<Uint8Array | string> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(
    public url: string,
    public protocols: string[],
  ) {}
  send(data: Uint8Array | string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = 3;
  }
  // Test helpers
  open(): void {
    this.onopen?.();
  }
  message(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent);
  }
}

function withToken(): void {
  localStorage.setItem("mm_token", "tok-123");
}

describe("frameIncrement", () => {
  it("prefixes a 4-byte little-endian sequence", () => {
    const payload = new Uint8Array([9, 8, 7]).buffer;
    const frame = frameIncrement(5, payload);
    expect(frame.byteLength).toBe(7);
    expect(new DataView(frame.buffer).getUint32(0, true)).toBe(5);
    expect(Array.from(frame.slice(4))).toEqual([9, 8, 7]);
  });
});

describe("LiveSocket sequence + resume", () => {
  it("assigns monotonic sequences and replays from the resume cursor in order", () => {
    withToken();
    let fake!: FakeWebSocket;
    const sock = new LiveSocket("m1", 1000, {}, (url, protocols) => {
      fake = new FakeWebSocket(url, protocols);
      return fake as unknown as WebSocket;
    });
    sock.connect();
    expect(fake.protocols).toEqual(["mm.live.v1", "tok-123"]);
    fake.open();

    // Three increments while open -> sent immediately, seq 0,1,2.
    sock.pushChunk(new Uint8Array([1]).buffer);
    sock.pushChunk(new Uint8Array([2]).buffer);
    sock.pushChunk(new Uint8Array([3]).buffer);
    const binarySends = fake.sent.filter((s) => s instanceof Uint8Array) as Uint8Array[];
    expect(binarySends.map((f) => new DataView(f.buffer).getUint32(0, true))).toEqual([
      0, 1, 2,
    ]);

    // A resume{next_seq:1} replays seq 1 and 2 (not 0), in order.
    fake.sent.length = 0;
    fake.message({ type: "resume", next_seq: 1 });
    const replayed = (fake.sent.filter((s) => s instanceof Uint8Array) as Uint8Array[]).map(
      (f) => new DataView(f.buffer).getUint32(0, true),
    );
    expect(replayed).toEqual([1, 2]);
  });

  it("routes transcript / asr frames and surfaces 1008 as auth-expired", () => {
    withToken();
    const onTranscript = vi.fn();
    const onAsrUnavailable = vi.fn();
    const onAuthExpired = vi.fn();
    let fake!: FakeWebSocket;
    const sock = new LiveSocket(
      "m2",
      1000,
      { onTranscript, onAsrUnavailable, onAuthExpired },
      (url, protocols) => {
        fake = new FakeWebSocket(url, protocols);
        return fake as unknown as WebSocket;
      },
    );
    sock.connect();
    fake.open();
    fake.message({ type: "transcript", result: { segments: [] } });
    fake.message({ type: "asr_unavailable" });
    expect(onTranscript).toHaveBeenCalledOnce();
    expect(onAsrUnavailable).toHaveBeenCalledOnce();
    // A policy-violation close (expired token) surfaces auth-expired, no reconnect.
    fake.onclose?.({ code: 1008 } as CloseEvent);
    expect(onAuthExpired).toHaveBeenCalledOnce();
  });
});

describe("LiveSocket reconnect limiting", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops reconnecting and fires onGiveUp after repeated pre-open rejections", () => {
    withToken();
    const onGiveUp = vi.fn();
    const fakes: FakeWebSocket[] = [];
    const sock = new LiveSocket(
      "m-giveup",
      1000,
      { onGiveUp },
      (url, protocols) => {
        const fake = new FakeWebSocket(url, protocols);
        fakes.push(fake);
        return fake as unknown as WebSocket;
      },
    );
    sock.connect();
    // Four handshakes rejected before open (server 403 surfaces as close 1006):
    // each schedules exactly one more attempt.
    for (let i = 0; i < 4; i++) {
      fakes[i].onclose?.({ code: 1006 } as CloseEvent);
      vi.advanceTimersByTime(1000);
    }
    expect(fakes).toHaveLength(5);
    expect(onGiveUp).not.toHaveBeenCalled();
    // The fifth rejection exhausts the budget: give-up fires, no more attempts.
    fakes[4].onclose?.({ code: 1006 } as CloseEvent);
    expect(onGiveUp).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(60_000);
    expect(fakes).toHaveLength(5);
  });

  it("resets the failure budget once a connection opens", () => {
    withToken();
    const onGiveUp = vi.fn();
    const fakes: FakeWebSocket[] = [];
    const sock = new LiveSocket(
      "m-reset",
      1000,
      { onGiveUp },
      (url, protocols) => {
        const fake = new FakeWebSocket(url, protocols);
        fakes.push(fake);
        return fake as unknown as WebSocket;
      },
    );
    sock.connect();
    fakes[0].onclose?.({ code: 1006 } as CloseEvent); // failure 1
    vi.advanceTimersByTime(1000);
    fakes[1].open(); // a successful open refreshes the budget
    for (let i = 1; i < 5; i++) {
      fakes[i].onclose?.({ code: 1006 } as CloseEvent);
      vi.advanceTimersByTime(1000);
    }
    // 4 consecutive failures under the refreshed budget: still retrying.
    expect(fakes).toHaveLength(6);
    expect(onGiveUp).not.toHaveBeenCalled();
  });
});
