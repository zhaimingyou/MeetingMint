import { apiDownload, apiRequest } from "./client";
import type {
  CreateMeetingResponse,
  DeleteMeetingResponse,
  MeetingDetail,
  MeetingListItem,
  MeetingMinutes,
  ResubmitResponse,
  SaveMinutesResponse,
  StatusResponse,
} from "./types";

/** POST /api/meetings (multipart, field name "file"). */
export function createMeeting(
  file: Blob,
  filename: string,
): Promise<CreateMeetingResponse> {
  const form = new FormData();
  form.append("file", file, filename);
  return apiRequest<CreateMeetingResponse>("/meetings", {
    method: "POST",
    body: form,
  });
}

/** GET /api/meetings (server-ordered, newest first). */
export function listMeetings(): Promise<MeetingListItem[]> {
  return apiRequest<MeetingListItem[]>("/meetings");
}

/** GET /api/meetings/{id} */
export function getMeeting(id: string): Promise<MeetingDetail> {
  return apiRequest<MeetingDetail>(`/meetings/${encodeURIComponent(id)}`);
}

/** GET /api/meetings/{id}/status */
export function getMeetingStatus(id: string): Promise<StatusResponse> {
  return apiRequest<StatusResponse>(
    `/meetings/${encodeURIComponent(id)}/status`,
  );
}

/** POST /api/meetings/{id}/resubmit */
export function resubmitMeeting(id: string): Promise<ResubmitResponse> {
  return apiRequest<ResubmitResponse>(
    `/meetings/${encodeURIComponent(id)}/resubmit`,
    { method: "POST" },
  );
}

/** PUT /api/meetings/{id}/minutes */
export function saveMinutes(
  id: string,
  minutes: MeetingMinutes,
): Promise<SaveMinutesResponse> {
  return apiRequest<SaveMinutesResponse>(
    `/meetings/${encodeURIComponent(id)}/minutes`,
    { method: "PUT", json: minutes },
  );
}

/** GET /api/meetings/{id}/export -> markdown blob + filename. */
export function exportMeeting(
  id: string,
  format: "md" | "docx" = "md",
): Promise<{ blob: Blob; filename: string | null }> {
  return apiDownload(
    `/meetings/${encodeURIComponent(id)}/export?format=${format}`,
  );
}

/** DELETE /api/meetings/{id} */
export function deleteMeeting(id: string): Promise<DeleteMeetingResponse> {
  return apiRequest<DeleteMeetingResponse>(
    `/meetings/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

export interface CorrectionResponse {
  corrected: string;
}

export function correctSelection(
  meetingId: string,
  text: string,
  context?: string,
): Promise<CorrectionResponse> {
  return apiRequest<CorrectionResponse>(
    `/meetings/${encodeURIComponent(meetingId)}/correct-selection`,
    { method: "POST", json: { text, context: context || undefined } },
  );
}

export interface BatchSegmentInput {
  index: number;
  text: string;
  speaker: string | null;
}

export interface BatchSegmentOutput {
  index: number;
  text: string;
}

export interface CorrectBatchResponse {
  segments: BatchSegmentOutput[];
}

export function correctBatch(
  meetingId: string,
  segments: BatchSegmentInput[],
): Promise<CorrectBatchResponse> {
  return apiRequest<CorrectBatchResponse>(
    `/meetings/${encodeURIComponent(meetingId)}/transcript/correct-batch`,
    { method: "POST", json: { segments } },
  );
}

export function updateTranscript(
  meetingId: string,
  segments: Array<{
    start: number;
    end: number;
    text: string;
    speaker: string | null;
  }>,
): Promise<{ id: string; updated: boolean; count: number }> {
  return apiRequest(
    `/meetings/${encodeURIComponent(meetingId)}/transcript`,
    { method: "PUT", json: { segments } },
  );
}
