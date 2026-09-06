/** Local filesystem storage for development and the local-only application. */
import { LocalStorage } from "./local";
import type { StorageAdapter } from "./types";

export * from "./types";
export { LocalStorage } from "./local";

let instance: StorageAdapter | null = null;

export function getStorage(): StorageAdapter {
  if (!instance) instance = new LocalStorage();
  return instance;
}

export function getLocalStorage(): LocalStorage {
  const storage = getStorage();
  if (!(storage instanceof LocalStorage)) {
    throw new Error("The local filesystem storage adapter is not active");
  }
  return storage;
}

/** Test-only seam: force a specific adapter instance. */
export function __setStorageForTests(adapter: StorageAdapter): void {
  instance = adapter;
}
