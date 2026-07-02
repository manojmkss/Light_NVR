import { apiFetch } from "./client";
import type { StorageConfig, StorageConfigUpdate, StorageHealth, StorageTestRequest, StorageTestResult } from "./types";

export function getStorageConfig(): Promise<StorageConfig> {
  return apiFetch<StorageConfig>("/storage/config");
}

export function updateStorageConfig(payload: StorageConfigUpdate): Promise<StorageConfig> {
  return apiFetch<StorageConfig>("/storage/config", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function testStorage(payload: StorageTestRequest): Promise<StorageTestResult> {
  return apiFetch<StorageTestResult>("/storage/test", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStorageHealth(): Promise<StorageHealth> {
  return apiFetch<StorageHealth>("/storage/health");
}
