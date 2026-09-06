/**
 * Guest identity (plan §10.3 as amended): an httpOnly cookie holding a random
 * ID plus an HMAC signature. The guest ID is the owner_key for job ownership,
 * idempotency scoping, and quotas — replaced by accounts post-MVP.
 */
import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

const COOKIE_NAME = "stemify_gid";
const MAX_AGE_SECONDS = 60 * 60 * 24 * 30; // 30 days

function secret(): string {
  const s = process.env.JOB_ACCESS_TOKEN_SECRET;
  if (!s) {
    throw new Error("JOB_ACCESS_TOKEN_SECRET is not set — cannot sign guest IDs.");
  }
  return s;
}

function sign(guestId: string): string {
  return createHmac("sha256", secret()).update(guestId).digest("base64url");
}

export function newGuestId(): string {
  return `gid_${crypto.randomUUID().replaceAll("-", "")}`;
}

function verify(guestId: string, signature: string): boolean {
  const expected = Buffer.from(sign(guestId));
  const provided = Buffer.from(signature);
  return expected.length === provided.length && timingSafeEqual(expected, provided);
}

/** Returns the verified guest ID, creating + setting the cookie when absent. */
export async function getOrCreateGuestId(): Promise<string> {
  const store = await cookies();
  const raw = store.get(COOKIE_NAME)?.value;
  if (raw) {
    const [guestId, signature] = raw.split(".");
    if (guestId && signature && verify(guestId, signature)) {
      return guestId;
    }
  }
  const guestId = newGuestId();
  store.set(COOKIE_NAME, `${guestId}.${sign(guestId)}`, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    maxAge: MAX_AGE_SECONDS,
    path: "/",
  });
  return guestId;
}
