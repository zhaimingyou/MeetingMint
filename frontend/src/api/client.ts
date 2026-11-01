/**
 * Typed fetch wrapper.
 *
 * - Injects the stored Bearer token on every request.
 * - Parses JSON / text responses.
 * - On a 401 it clears the stored token and signals a global logout so the
 *   AuthGuard can redirect to /login.
 */

// TEMPORARY: dev-only preview mode (remove with src/preview.ts to restore real API).
import {
  PREVIEW_ENABLED,
  getPreviewResponse,
  getPreviewDownload,
} from "../preview";

const BASE_PATH = "/api";
const TOKEN_KEY = "mm_token";
const USER_KEY = "mm_user";
const ADMIN_KEY = "mm_is_admin";

let onUnauthorized: (() => void) | null = null;

/** Register a handler invoked whenever the API returns 401. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function getStoredUser(): string | null {
  return localStorage.getItem(USER_KEY);
}

export function setStoredUser(username: string): void {
  localStorage.setItem(USER_KEY, username);
}

export function clearStoredUser(): void {
  localStorage.removeItem(USER_KEY);
}

export function getStoredIsAdmin(): boolean {
  return localStorage.getItem(ADMIN_KEY) === "1";
}

export function setStoredIsAdmin(isAdmin: boolean): void {
  localStorage.setItem(ADMIN_KEY, isAdmin ? "1" : "0");
}

export function clearStoredIsAdmin(): void {
  localStorage.removeItem(ADMIN_KEY);
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail?: string;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

interface RequestOptions {
  method?: string;
  /** JSON body; serialized and sent with application/json. */
  json?: unknown;
  /** Raw body (e.g. FormData); Content-Type is left to the browser. */
  body?: BodyInit;
  headers?: Record<string, string>;
  /** Expected response handling. Defaults to "json". */
  parse?: "json" | "text" | "blob" | "none";
  signal?: AbortSignal;
}

async function parseErrorBody(res: Response): Promise<{ message: string; detail?: string; body: unknown }> {
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const data = (await res.json()) as Record<string, unknown>;
      const detail =
        typeof data.detail === "string" ? data.detail : undefined;
      const message = detail ?? `请求失败（${res.status}）`;
      return { message, detail, body: data };
    } catch {
      return { message: `请求失败（${res.status}）`, body: null };
    }
  }
  try {
    const text = await res.text();
    return { message: text || `请求失败（${res.status}）`, body: text };
  } catch {
    return { message: `请求失败（${res.status}）`, body: null };
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", json, body, parse = "json", signal } = options;

  // TEMPORARY: serve mock data in dev preview mode (no backend needed).
  if (PREVIEW_ENABLED) {
    const preview = getPreviewResponse(path, method);
    if (preview !== undefined) {
      return preview as T;
    }
  }

  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  const token = getToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  let requestBody: BodyInit | undefined = body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    requestBody = JSON.stringify(json);
  }

  const res = await fetch(`${BASE_PATH}${path}`, {
    method,
    headers,
    body: requestBody,
    signal,
  });

  if (res.status === 401) {
    clearToken();
    clearStoredUser();
    clearStoredIsAdmin();
    if (onUnauthorized) onUnauthorized();
    const { message, detail, body: errBody } = await parseErrorBody(res);
    throw new ApiError(401, message, errBody, detail);
  }

  if (!res.ok) {
    const { message, detail, body: errBody } = await parseErrorBody(res);
    throw new ApiError(res.status, message, errBody, detail);
  }

  if (parse === "none" || res.status === 204) {
    return undefined as T;
  }
  if (parse === "text") {
    return (await res.text()) as T;
  }
  if (parse === "blob") {
    return (await res.blob()) as T;
  }
  return (await res.json()) as T;
}

/** Fetch a Blob plus the suggested filename from Content-Disposition. */
export async function apiDownload(
  path: string,
): Promise<{ blob: Blob; filename: string | null }> {
  // TEMPORARY: dev preview mode returns a sample export without a backend.
  if (PREVIEW_ENABLED) {
    return getPreviewDownload();
  }

  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE_PATH}${path}`, { headers });

  if (res.status === 401) {
    clearToken();
    clearStoredUser();
    clearStoredIsAdmin();
    if (onUnauthorized) onUnauthorized();
    throw new ApiError(401, "未授权", null);
  }
  if (!res.ok) {
    const { message, detail, body } = await parseErrorBody(res);
    throw new ApiError(res.status, message, body, detail);
  }

  const blob = await res.blob();
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : null;
  return { blob, filename };
}
