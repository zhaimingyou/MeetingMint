import type { JobStatus } from "../api/types";
import { statusLabel, statusTone } from "../utils/status";

const TONE_CLASS: Record<string, string> = {
  success: "pill--success",
  processing: "pill--processing",
  failed: "pill--failed",
  neutral: "pill--neutral",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  const tone = statusTone(status);
  return (
    <span className={`pill ${TONE_CLASS[tone]}`} data-testid="status-badge">
      {statusLabel(status)}
    </span>
  );
}
