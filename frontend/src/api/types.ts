export type JobStatus =
  | "live"
  | "queued"
  | "preprocessing"
  | "transcribing"
  | "generating"
  | "completed"
  | "failed";

/** Supported audio container/codec formats accepted by the backend. */
export const SUPPORTED_AUDIO_FORMATS = ["mp3", "wav", "m4a", "webm"] as const;
export type SupportedAudioFormat = (typeof SUPPORTED_AUDIO_FORMATS)[number];

/** Accept attribute for the upload file picker. */
export const SUPPORTED_AUDIO_ACCEPT =
  ".mp3,.wav,.m4a,.webm,audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/x-m4a,audio/webm";

export interface LoginResponse {
  token: string;
  token_type: string;
  is_admin?: boolean;
}

/** GET /api/auth/me */
export interface MeResponse {
  username: string;
  is_admin: boolean;
}

/** POST /api/auth/register -> the newly created account (no token, no session). */
export interface RegisterResponse {
  id: string;
  username: string;
  is_admin: boolean;
  disabled: boolean;
  created_at: string;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker: string | null;
}

export interface Transcript {
  segments: TranscriptSegment[];
}

export interface ActionItem {
  description: string;
  owner: string;
}

export interface MeetingMinutes {
  summary: string;
  topics: string[];
  decisions: string[];
  action_items: ActionItem[];
}

/** Row shape returned by GET /api/meetings. */
export interface MeetingListItem {
  id: string;
  title: string;
  status: JobStatus;
  created_at: string;
}

/** Full record returned by GET /api/meetings/{id}. */
export interface MeetingDetail {
  id: string;
  title: string;
  status: JobStatus;
  created_at: string;
  transcript: Transcript | null;
  minutes: MeetingMinutes | null;
}

/** Response of POST /api/meetings. */
export interface CreateMeetingResponse {
  id: string;
  status: JobStatus;
  title: string;
}

export interface StatusResponse {
  status: JobStatus;
  error_description?: string;
}

export interface ResubmitResponse {
  id: string;
  status: JobStatus;
}

export interface SaveMinutesResponse {
  id: string;
  saved: boolean;
}

export interface DeleteMeetingResponse {
  id: string;
  deleted: boolean;
}

/** Response of POST /api/meetings/live (start a Live_Meeting_Session). */
export interface StartLiveResponse {
  id: string;
  status: JobStatus;
  title: string;
  /** Seconds between Audio_Increment transmissions (MediaRecorder timeslice). */
  send_interval: number;
  /** Upper bound (seconds) on the reconnect cadence after a dropped socket. */
  reconnect_interval: number;
}

/** One incremental streaming-ASR segment shown in the live preview. */
export interface LiveSegment {
  start: number;
  end: number;
  text: string;
  speaker: string | null;
  /** True = Finalized_Segment (stable); false = Partial_Segment (provisional). */
  is_final: boolean;
}

/** One incremental transcript update pushed over the Streaming_Connection. */
export interface LiveTranscriptResult {
  segments: LiveSegment[];
}


// --------------------------------------------------------------------------- #
// Admin console (admin-console feature)
// --------------------------------------------------------------------------- #

/** One runtime-editable configuration field as returned by GET /api/admin/config. */
export interface ConfigField {
  key: string;
  secret: boolean;
  source: "runtime" | "env";
  editable: true;
  /** Present for non-secret fields only. */
  value?: string | number | boolean | string[] | null;
  /** Present for secret fields only. */
  configured?: boolean;
}

/** One restart-only (read-only) setting. */
export interface RestartOnlyField {
  key: string;
  editable: false;
  secret?: boolean;
  value?: string | number | boolean | string[] | null;
  configured?: boolean;
}

export interface ConfigResponse {
  editable: ConfigField[];
  restart_only: RestartOnlyField[];
}

export type ConnectivityStatus = "ok" | "warn" | "fail" | "skip";

export interface ConnectivityResult {
  asr: { status: ConnectivityStatus; message: string };
  llm: { status: ConnectivityStatus; message: string };
}

export interface AdminUserRow {
  id: string;
  username: string;
  is_admin: boolean;
  disabled: boolean;
  created_at: string;
}

export interface AdminJobRow {
  meeting_id: string;
  title: string;
  owner_username: string;
  status: JobStatus;
  created_at: string;
  error_description?: string | null;
}

export interface AuditEntry {
  id: string;
  actor_username: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: unknown;
  created_at: string;
}

export interface Paged<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}
