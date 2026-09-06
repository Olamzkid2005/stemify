/**
 * Namespace guard: only source and result objects may ever be signed,
 * downloaded, or deleted — nothing else in the bucket (plan §15.2).
 */
import { ObjectKeyError } from "./types";

export function assertSignableKey(objectKey: string): void {
  if (objectKey.startsWith("sources/")) return;
  if (objectKey.startsWith("results/")) return;
  throw new ObjectKeyError(objectKey, "sources/ or results/");
}
