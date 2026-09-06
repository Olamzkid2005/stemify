/**
 * Storage selector (plan Task 4). Production/preview use R2; tests and local
 * development without credentials fall back to the in-memory fake.
 */
import { FakeStorage } from "./fake";
import { R2Storage, r2ConfigFromEnv } from "./r2";
import type { StorageAdapter } from "./types";

export * from "./types";
export { FakeStorage } from "./fake";
export { R2Storage, r2ConfigFromEnv } from "./r2";

let instance: StorageAdapter | null = null;

export function getStorage(): StorageAdapter {
  if (instance) return instance;
  const required = [
    process.env.STORAGE_ACCOUNT_ID,
    process.env.STORAGE_ACCESS_KEY_ID,
    process.env.STORAGE_SECRET_ACCESS_KEY,
    process.env.STORAGE_BUCKET,
  ];
  instance =
    required.every(Boolean)
      ? new R2Storage(r2ConfigFromEnv())
      : new FakeStorage();
  return instance;
}
