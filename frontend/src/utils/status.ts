import type { JobStatus } from "../api/types";

/** Simplified-Chinese labels for each job status. */
export const STATUS_LABELS: Record<JobStatus, string> = {
  live: "进行中",
  queued: "排队中",
  preprocessing: "预处理中",
  transcribing: "转写中",
  generating: "生成中",
  completed: "已完成",
  failed: "失败",
};

/** Statuses that are still in progress and warrant polling. */
export const IN_PROGRESS_STATUSES: JobStatus[] = [
  "live",
  "queued",
  "preprocessing",
  "transcribing",
  "generating",
];

export function isInProgress(status: JobStatus): boolean {
  return IN_PROGRESS_STATUSES.includes(status);
}

export function statusLabel(status: JobStatus): string {
  return STATUS_LABELS[status] ?? status;
}

export type StatusTone = "success" | "processing" | "failed" | "neutral";

export function statusTone(status: JobStatus): StatusTone {
  if (status === "completed") return "success";
  if (status === "failed") return "failed";
  if (isInProgress(status)) return "processing";
  return "neutral";
}
