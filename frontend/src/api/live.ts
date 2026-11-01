/**
 * Live meeting API + WebSocket client (realtime-meeting-transcription).
 *
 * - REST helpers to start/end a Live_Meeting_Session.
 * - A pure preview reducer (`applyResult`) that maintains the Live_Transcript_Preview
 *   ordering invariants (Property 42): Finalized_Segments ascending by start,
 *   Partial_Segments after them, superseded partials replaced, finalized stable.
 * - `LiveSocket`: the Streaming_Connection client. It carries the session token
 *   in the WebSocket subprotocol, assigns a monotonic sequence to each captured
 *   Audio_Increment, retains every increment in a local FIFO buffer, honors the
 *   server `resume {next_seq}` frame by replaying from there in order (so the
 *   recording stays exactly-once across reconnects), reconnects with backoff,
 *   and surfaces an expired-token rejection (close 1008) without discarding the
 *   buffered audio so the view can re-authenticate and resume.
 */

import { apiRequest, getToken } from "./client";
import type {
  JobStatus,
  LiveSegment,
  LiveTranscriptResult,
  StartLiveResponse,
} from "./types";

export const LIVE_SUBPROTOCOL = "mm.live.v1";

/** POST /api/meetings/live -- start a Live_Meeting_Session. */
export function startLiveMeeting(): Promise<StartLiveResponse> {
  return apiRequest<StartLiveResponse>("/meetings/live", { method: "POST" });
}

/** POST /api/meetings/{id}/end -- finalize a Live_Meeting_Session (REST fallback). */
export function endLiveMeeting(id: string): Promise<{ id: string; status: JobStatus }> {
  return apiRequest<{ id: string; status: JobStatus }>(
    `/meetings/${encodeURIComponent(id)}/end`,
    { method: "POST" },
  );
}

// --------------------------------------------------------------------------- #
// Preview reduction (Property 42) -- pure and unit-tested.
// --------------------------------------------------------------------------- #
export interface PreviewState {
  finalized: LiveSegment[];
  partials: LiveSegment[];
}

export function emptyPreview(): PreviewState {
  return { finalized: [], partials: [] };
}

/**
 * Fold one incremental result into the preview.
 *
 * Finalized_Segments accumulate (keyed by start so a later finalize for the
 * same start replaces in place) and are kept sorted ascending; the result's
 * Partial_Segments replace any prior partials (superseded partials never
 * remain). When a result carries any segment its partials become authoritative;
 * an empty result leaves the preview unchanged.
 */
export function applyResult(
  state: PreviewState,
  result: LiveTranscriptResult,
): PreviewState {
  if (!result.segments || result.segments.length === 0) {
    return state;
  }
  const byStart = new Map<number, LiveSegment>();
  for (const seg of state.finalized) byStart.set(seg.start, seg);
  const newPartials: LiveSegment[] = [];
  for (const seg of result.segments) {
    if (seg.is_final) {
      byStart.set(seg.start, seg);
    } else {
      newPartials.push(seg);
    }
  }
  const finalized = [...byStart.values()].sort(
    (a, b) => a.start - b.start || a.end - b.end,
  );
  return { finalized, partials: newPartials };
}

/** Flatten the preview for display: finalized (ascending) then partials. */
export function previewSegments(state: PreviewState): LiveSegment[] {
  return [...state.finalized, ...state.partials];
}

// --------------------------------------------------------------------------- #
// LiveSocket -- the Streaming_Connection client.
// --------------------------------------------------------------------------- #
const SEQ_PREFIX_BYTES = 4;

/** Frame one Audio_Increment: 4-byte little-endian seq prefix + payload bytes. */
export function frameIncrement(seq: number, payload: ArrayBuffer): Uint8Array {
  const out = new Uint8Array(SEQ_PREFIX_BYTES + payload.byteLength);
  new DataView(out.buffer).setUint32(0, seq, true);
  out.set(new Uint8Array(payload), SEQ_PREFIX_BYTES);
  return out;
}

export interface LiveSocketHandlers {
  onTranscript?: (result: LiveTranscriptResult) => void;
  onAsrUnavailable?: () => void;
  onAsrAvailable?: () => void;
  /** The (re)connection was rejected for an expired/invalidated token (1008). */
  onAuthExpired?: () => void;
  /**
   * Too many consecutive handshake rejections before open (e.g. the meeting
   * was finalized server-side, so every reconnect is refused). Reconnecting
   * has stopped; the user must start a new meeting.
   */
  onGiveUp?: () => void;
  onOpen?: () => void;
  onClose?: () => void;
}

type SocketFactory = (url: string, protocols: string[]) => WebSocket;

const POLICY_VIOLATION_CLOSE = 1008;

/**
 * How many consecutive rejected handshakes (close before open) are tolerated
 * before reconnecting stops. A finalized/failed meeting refuses every attempt
 * with an HTTP 403 that surfaces as close code 1006; without a cap the client
 * would retry forever.
 */
const MAX_CONNECT_FAILURES = 5;

export class LiveSocket {
  private ws: WebSocket | null = null;
  private readonly buffer: { seq: number; frame: Uint8Array }[] = [];
  private nextSeqToAssign = 0;
  private closedByClient = false;
  private authExpired = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private openedThisConnection = false;
  private consecutiveConnectFailures = 0;
  private gaveUp = false;

  constructor(
    private readonly meetingId: string,
    private readonly reconnectIntervalMs: number,
    private readonly handlers: LiveSocketHandlers = {},
    private readonly socketFactory: SocketFactory = (url, protocols) =>
      new WebSocket(url, protocols),
  ) {}

  private url(): string {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}/api/meetings/${encodeURIComponent(
      this.meetingId,
    )}/live`;
  }

  /** Open (or re-open) the connection with the current stored token. */
  connect(): void {
    if (this.gaveUp) return;
    const token = getToken();
    if (!token) {
      this.handlers.onAuthExpired?.();
      return;
    }
    this.authExpired = false;
    this.openedThisConnection = false;
    const ws = this.socketFactory(this.url(), [LIVE_SUBPROTOCOL, token]);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      this.openedThisConnection = true;
      this.handlers.onOpen?.();
    };
    ws.onmessage = (ev: MessageEvent) => this.handleMessage(ev);
    ws.onclose = (ev: CloseEvent) => this.handleClose(ev);
    ws.onerror = () => {
      /* a close event follows; reconnection is handled there */
    };
    this.ws = ws;
  }

  private handleMessage(ev: MessageEvent): void {
    if (typeof ev.data !== "string") return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (msg.type) {
      case "resume":
        this.replayFrom(Number(msg.next_seq) || 0);
        break;
      case "transcript":
        this.handlers.onTranscript?.(msg.result as LiveTranscriptResult);
        break;
      case "asr_unavailable":
        this.handlers.onAsrUnavailable?.();
        break;
      case "asr_available":
        this.handlers.onAsrAvailable?.();
        break;
      default:
        break;
    }
  }

  private handleClose(ev: CloseEvent): void {
    this.handlers.onClose?.();
    if (this.closedByClient) return;
    if (ev.code === POLICY_VIOLATION_CLOSE) {
      // Token expired/invalidated mid-meeting: retain the buffer (do NOT drop
      // captured audio) and let the view re-authenticate and resume.
      this.authExpired = true;
      this.handlers.onAuthExpired?.();
      return;
    }
    if (this.openedThisConnection) {
      // A dropped live connection is transient: reset the rejection budget.
      this.consecutiveConnectFailures = 0;
    } else {
      // Rejected before open (e.g. server 403 -> 1006): the meeting is likely
      // finalized or the token invalid. Count it; stop after the budget is
      // exhausted instead of retrying forever.
      this.consecutiveConnectFailures += 1;
      if (this.consecutiveConnectFailures >= MAX_CONNECT_FAILURES) {
        this.gaveUp = true;
        this.handlers.onGiveUp?.();
        return;
      }
    }
    // Transient drop: reconnect with backoff; the buffer is retained so the
    // server's resume frame can replay any missed increments in order.
    this.reconnectTimer = setTimeout(() => this.connect(), this.reconnectIntervalMs);
  }

  /** Replay buffered increments from ``nextSeq`` (server resume cursor), in order. */
  private replayFrom(nextSeq: number): void {
    if (!this.isOpen()) return;
    for (const item of this.buffer) {
      if (item.seq >= nextSeq) {
        this.ws!.send(item.frame);
      }
    }
  }

  private isOpen(): boolean {
    // OPEN is always 1 per the WHATWG spec; avoid referencing the global
    // ``WebSocket`` constant so an injected fake socket works under jsdom.
    return this.ws != null && this.ws.readyState === 1;
  }

  /** Capture one Audio_Increment: assign a sequence, retain it, and send if open. */
  pushChunk(payload: ArrayBuffer): void {
    const seq = this.nextSeqToAssign++;
    const frame = frameIncrement(seq, payload);
    this.buffer.push({ seq, frame });
    if (this.isOpen()) {
      this.ws!.send(frame);
    }
  }

  /** Send a JSON control frame (keepalive / end) when connected. */
  private sendControl(obj: Record<string, unknown>): void {
    if (this.isOpen()) {
      this.ws!.send(JSON.stringify(obj));
    }
  }

  keepalive(): void {
    this.sendControl({ type: "keepalive" });
  }

  /** End the meeting: send the end frame and close intentionally (no reconnect). */
  end(): void {
    this.closedByClient = true;
    this.sendControl({ type: "end" });
    this.close();
  }

  /** Close the socket without reconnecting (intentional teardown). */
  close(): void {
    this.closedByClient = true;
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws != null) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  get pendingAuthRefresh(): boolean {
    return this.authExpired;
  }
}
