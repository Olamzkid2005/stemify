import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

/**
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh temp
 * directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { POST } from "@/app/api/uploads/route";

/**
 * The upload route enforces this machine's effective size cap (plan Section 9).
 *
 * The handler is called directly. What is being pinned is the wiring, not the
 * number: a cap read from `.env` has to reach the check, and the refusal has to
 * carry the cap so a client rendering a stale one can explain itself
 * (lib/limits.ts `uploadErrorMessage`).
 *
 * Only the refusal path is covered here: accepting a file continues into the
 * guest-cookie store, which needs an app-router request scope that does not
 * exist outside the server.
 */

function uploadRequest(bytes: number): Request {
  const form = new FormData();
  form.append("file", new File([new Uint8Array(bytes)], "song.mp3", { type: "audio/mpeg" }));
  return new Request("http://localhost/api/uploads", { method: "POST", body: form });
}

describe("POST /api/uploads size cap (plan Section 9)", () => {
  afterEach(() => {
    delete process.env.MAX_UPLOAD_BYTES;
  });

  it("refuses a file over the configured cap, reporting the cap it used", async () => {
    process.env.MAX_UPLOAD_BYTES = "1024";
    const response = await POST(uploadRequest(4096));
    assert.equal(response.status, 413);
    assert.deepEqual(await response.json(), { error: "file_too_large", limitBytes: 1024 });
  });

  it("reports the cap it was configured with, not the shipped default", async () => {
    process.env.MAX_UPLOAD_BYTES = "8192";
    const response = await POST(uploadRequest(9000));
    assert.equal(response.status, 413);
    assert.deepEqual(await response.json(), { error: "file_too_large", limitBytes: 8192 });
  });
});
