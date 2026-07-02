import { apiFetch } from "./client";
import type { SecuritySettings, User } from "./types";

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface SetupStatus {
  setup_required: boolean;
}

export function getSetupStatus(): Promise<SetupStatus> {
  return apiFetch<SetupStatus>("/auth/setup-status");
}

export function runSetup(username: string, password: string): Promise<TokenResponse> {
  return apiFetch<TokenResponse>("/auth/setup", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function login(username: string, password: string): Promise<TokenResponse> {
  return apiFetch<TokenResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function changeMyPassword(currentPassword: string, newPassword: string): Promise<void> {
  return apiFetch<void>("/auth/me/password", {
    method: "PUT",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

export function resetUserPassword(userId: number, newPassword: string): Promise<void> {
  return apiFetch<void>(`/auth/users/${userId}/password`, {
    method: "PUT",
    body: JSON.stringify({ new_password: newPassword }),
  });
}

export function getMe(): Promise<User> {
  return apiFetch<User>("/auth/me");
}

export function listUsers(): Promise<User[]> {
  return apiFetch<User[]>("/auth/users");
}

export function createUser(payload: {
  username: string;
  password: string;
  role: string;
  email?: string;
}): Promise<User> {
  return apiFetch<User>("/auth/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteUser(userId: number): Promise<void> {
  return apiFetch<void>(`/auth/users/${userId}`, { method: "DELETE" });
}

export function getSecuritySettings(): Promise<SecuritySettings> {
  return apiFetch<SecuritySettings>("/auth/security-settings");
}

export function updateSecuritySettings(payload: Partial<SecuritySettings>): Promise<SecuritySettings> {
  return apiFetch<SecuritySettings>("/auth/security-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}
