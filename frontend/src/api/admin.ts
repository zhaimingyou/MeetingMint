/**
 * Admin_Console API client (admin-console feature).
 *
 * Thin typed wrappers over the /api/admin/* endpoints. All requests carry the
 * stored bearer token via the shared `apiRequest`; the backend enforces the
 * administrator role regardless of the frontend gate.
 */

import { apiRequest } from "./client";
import type {
  AdminJobRow,
  AdminUserRow,
  AuditEntry,
  ConfigResponse,
  ConnectivityResult,
  Paged,
} from "./types";

// --- model configuration -----------------------------------------------------

export function getConfig(): Promise<ConfigResponse> {
  return apiRequest<ConfigResponse>("/admin/config");
}

/** PUT /api/admin/config. `null` resets a field; omit a secret to leave it. */
export function updateConfig(
  changes: Record<string, unknown>,
): Promise<ConfigResponse> {
  return apiRequest<ConfigResponse>("/admin/config", {
    method: "PUT",
    json: changes,
  });
}

export function testConnectivity(): Promise<ConnectivityResult> {
  return apiRequest<ConnectivityResult>("/admin/config/test", {
    method: "POST",
  });
}

// --- users -------------------------------------------------------------------

export function listUsers(): Promise<AdminUserRow[]> {
  return apiRequest<AdminUserRow[]>("/admin/users");
}

export function createUser(
  username: string,
  password: string,
  isAdmin: boolean,
): Promise<AdminUserRow> {
  return apiRequest<AdminUserRow>("/admin/users", {
    method: "POST",
    json: { username, password, is_admin: isAdmin },
  });
}

export function updateUser(
  id: string,
  body: { is_admin?: boolean; disabled?: boolean },
): Promise<AdminUserRow> {
  return apiRequest<AdminUserRow>(`/admin/users/${encodeURIComponent(id)}`, {
    method: "PUT",
    json: body,
  });
}

export function resetUserPassword(
  id: string,
  password: string,
): Promise<{ id: string; password_reset: boolean }> {
  return apiRequest(`/admin/users/${encodeURIComponent(id)}/password`, {
    method: "POST",
    json: { password },
  });
}

export function deleteUser(
  id: string,
): Promise<{ id: string; deleted: boolean }> {
  return apiRequest(`/admin/users/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// --- jobs --------------------------------------------------------------------

export function listJobs(params: {
  status?: string;
  page?: number;
  pageSize?: number;
}): Promise<Paged<AdminJobRow>> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.page) q.set("page", String(params.page));
  if (params.pageSize) q.set("page_size", String(params.pageSize));
  const qs = q.toString();
  return apiRequest<Paged<AdminJobRow>>(`/admin/jobs${qs ? `?${qs}` : ""}`);
}

export function resubmitJob(
  meetingId: string,
): Promise<{ id: string; status: string }> {
  return apiRequest(`/admin/jobs/${encodeURIComponent(meetingId)}/resubmit`, {
    method: "POST",
  });
}

// --- audit -------------------------------------------------------------------

export function listAudit(params: {
  page?: number;
  pageSize?: number;
}): Promise<Paged<AuditEntry>> {
  const q = new URLSearchParams();
  if (params.page) q.set("page", String(params.page));
  if (params.pageSize) q.set("page_size", String(params.pageSize));
  const qs = q.toString();
  return apiRequest<Paged<AuditEntry>>(`/admin/audit${qs ? `?${qs}` : ""}`);
}
