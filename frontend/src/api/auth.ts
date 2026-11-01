import { apiRequest } from "./client";
import type { LoginResponse, MeResponse, RegisterResponse } from "./types";

/** POST /api/auth/login -> { token, token_type, is_admin } */
export function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  return apiRequest<LoginResponse>("/auth/login", {
    method: "POST",
    json: { username, password },
  });
}

/** POST /api/auth/register -> { id, username, is_admin, disabled, created_at } */
export function register(
  username: string,
  password: string,
): Promise<RegisterResponse> {
  return apiRequest<RegisterResponse>("/auth/register", {
    method: "POST",
    json: { username, password },
  });
}

/** GET /api/auth/me -> { username, is_admin } */
export function getMe(): Promise<MeResponse> {
  return apiRequest<MeResponse>("/auth/me");
}

/** POST /api/auth/password -> { changed: true } */
export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ changed: boolean }> {
  return apiRequest<{ changed: boolean }>("/auth/password", {
    method: "POST",
    json: { current_password: currentPassword, new_password: newPassword },
  });
}

/** POST /api/auth/logout -> 204 */
export function logout(): Promise<void> {
  return apiRequest<void>("/auth/logout", {
    method: "POST",
    parse: "none",
  });
}
