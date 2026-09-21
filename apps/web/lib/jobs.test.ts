import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

/**
 * Job creation integration tests for the local SQLite queue.
 * Uses the in-memory fake storage adapter and the local database file.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import { FakeStorage } from "@/lib/storage/fake";
import { __setStorageForTests } from "@/lib/storage";
import { createJob, idempotencyHash } from "@/lib/jobs";

// These tests exercise idempotency/ownership, not the Task 13 active-job limit;
// lift the limit so multiple jobs for one owner don't trip 429s here.
process.env.MAX_ACTIVE_JOBS = "100";
// Spotify is opt-in per machine (lib/capabilities.ts); the link tests below are
// about the allowlist, so treat this machine as Spotify-capable. The capability
// gate has its own test, which flips this off for the duration.
process.env.STEMIFY_SPOTIFY_ENABLED = "1";

const OWNER = "gid_testowner0000000001";
const UPLOAD_ID = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
const OBJECT_KEY = `sources/${UPLOAD_ID}/song.mp3`;

const validBody: Record<string, unknown> = {
  source: { type: "upload", uploadId: UPLOAD_ID, objectKey: OBJECT_KEY, filename: "song.mp3" },
  mode: "vocals_instrumental",
  outputFormat: "mp3",
  idempotencyKey: "client-key-0123456789abcdef",
};

describe("createJob", () => {
  before(() => {
    const storage = new FakeStorage();
    __setStorageForTests(storage);
    storage.put(OBJECT_KEY, Buffer.alloc(2048));
    db.run(
      `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
      UPLOAD_ID,
      OWNER,
      "song.mp3",
      OBJECT_KEY,
      2048,
      Date.now() + 60_000,
    );
  });

  after(() => {
    delete process.env.MAX_ACTIVE_JOBS;
    delete process.env.STEMIFY_SPOTIFY_ENABLED;
    db.run("DELETE FROM jobs WHERE owner_key = ?", OWNER);
    db.run("DELETE FROM uploads WHERE owner_key = ?", OWNER);
    closeDatabase();
  });

  it("creates a queued job for a valid uploaded object", async () => {
    const result = await createJob({ ownerKey: OWNER, body: structuredClone(validBody) });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.status, 201);
      assert.match(result.job.id, /^job_[a-f0-9]{32}$/);
      assert.equal(result.job.status, "queued");
    }
  });

  it("stores a custom selection with the custom mode", async () => {
    const custom = await createJob({
      ownerKey: OWNER,
      body: {
        ...structuredClone(validBody),
        mode: "custom",
        stemSelection: ["vocals", "drums", "instrumental"],
        idempotencyKey: "client-key-custom00000001",
      },
    });
    assert.equal(custom.ok, true);
    const customRow = db.get<{ mode: string; stem_selection: string | null }>(
      "SELECT mode, stem_selection FROM jobs WHERE id = ?",
      (custom as { ok: true; job: { id: string } }).job.id,
    );
    assert.equal(customRow?.mode, "custom");
    assert.equal(customRow?.stem_selection, '["vocals","drums","instrumental"]');
  });

  it("refuses junk selections instead of building the wrong archive", async () => {
    for (const stemSelection of [[], ["piano"], ["drums", "drums"], "vocals", ["vocals", 5]]) {
      const result = await createJob({
        ownerKey: OWNER,
        body: {
          ...structuredClone(validBody),
          mode: "custom",
          stemSelection,
          idempotencyKey: `client-key-bad${Math.random().toString(16).slice(2, 10)}`,
        },
      });
      assert.equal(result.ok, false, `expected refusal for ${JSON.stringify(stemSelection)}`);
      if (!result.ok) assert.equal(result.status, 400);
    }
    // A selection on a fixed mode disagrees with what that mode computes,
    // and custom without one is meaningless: the contract forbids both.
    const fixed = await createJob({
      ownerKey: OWNER,
      body: {
        ...structuredClone(validBody),
        stemSelection: ["vocals"],
        idempotencyKey: "client-key-fixed00000001",
      },
    });
    assert.equal(fixed.ok, false);
    if (!fixed.ok) assert.equal(fixed.status, 400);
    const bare = await createJob({
      ownerKey: OWNER,
      body: {
        ...structuredClone(validBody),
        mode: "custom",
        idempotencyKey: "client-key-bare000000001",
      },
    });
    assert.equal(bare.ok, false);
    if (!bare.ok) assert.equal(bare.status, 400);
  });

  it("returns the same job for a repeated idempotency key", async () => {
    const body = { ...structuredClone(validBody), idempotencyKey: "client-key-repeat-000000001" };
    const first = await createJob({ ownerKey: OWNER, body });
    const second = await createJob({ ownerKey: OWNER, body: structuredClone(body) });
    assert.equal(first.ok && second.ok, true);
    if (first.ok && second.ok) {
      assert.equal(first.status, 201);
      assert.equal(second.status, 200);
      assert.equal(first.job.id, second.job.id);
    }
  });

  it("rejects invalid modes and formats", async () => {
    const bad = { ...structuredClone(validBody), mode: "karaoke" };
    const result = await createJob({ ownerKey: OWNER, body: bad });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });

  it("rejects uploads outside the caller's session", async () => {
    const evil = structuredClone(validBody) as { source: Record<string, unknown> };
    evil.source.uploadId = "upl_ffffffffffffffffffffffffffffffff";
    const result = await createJob({ ownerKey: OWNER, body: evil });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 403);
  });

  it("rejects uploads whose object never arrived", async () => {
    const missing = structuredClone(validBody) as {
      source: Record<string, unknown>;
      idempotencyKey: string;
    };
    missing.idempotencyKey = "client-key-ffffffffffffffff";
    missing.source.uploadId = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
    missing.source.objectKey = `sources/${missing.source.uploadId}/song.mp3`;
    const result = await createJob({ ownerKey: OWNER, body: missing });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 403);
  });

  it("rejects YouTube URLs outside the allowlisted hosts", async () => {
    const evil = structuredClone(validBody) as { source: Record<string, unknown> };
    evil.source = { type: "youtube", url: "https://evil.example.com/watch?v=x" };
    const result = await createJob({ ownerKey: OWNER, body: evil });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });

  it("creates a queued job for an allowlisted Spotify track link", async () => {
    const body = {
      ...structuredClone(validBody),
      source: { type: "spotify", url: "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" },
      idempotencyKey: "client-key-spotify-000001",
    };
    const result = await createJob({ ownerKey: OWNER, body });
    assert.equal(result.ok, true);
    if (!result.ok) return;
    assert.equal(result.status, 201);
    const row = db.get<{
      source_type: string;
      source_url: string | null;
      source_object_key: string | null;
    }>("SELECT source_type, source_url, source_object_key FROM jobs WHERE id = ?", result.job.id);
    assert.equal(row?.source_type, "spotify");
    assert.equal(row?.source_url, "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT");
    assert.equal(row?.source_object_key, null);
  });

  it("accepts a spotify:track: URI and the /intl-xx/ link form", async () => {
    for (const [index, url] of [
      "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
      "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=abc",
    ].entries()) {
      const body = {
        ...structuredClone(validBody),
        source: { type: "spotify", url },
        idempotencyKey: `client-key-spotify-ok-00000${index}`,
      };
      const result = await createJob({ ownerKey: OWNER, body });
      assert.equal(result.ok, true, `expected acceptance: ${url}`);
    }
  });

  it("rejects Spotify links that are not a single track", async () => {
    for (const [index, url] of [
      "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT",
      "https://open.spotify.com/playlist/4cOdK2wGLETKBW3PvgPWqT",
      "https://open.spotify.com/track/short",
      "https://evil.example.com/track/4cOdK2wGLETKBW3PvgPWqT",
      // Userinfo would make URL.hostname read "evil.example"; the policy must
      // reject the whole link rather than let it through on the prefix.
      "https://open.spotify.com@evil.example/track/4cOdK2wGLETKBW3PvgPWqT",
    ].entries()) {
      const body = {
        ...structuredClone(validBody),
        source: { type: "spotify", url },
        idempotencyKey: `client-key-spotify-bad-000${index}`,
      };
      const result = await createJob({ ownerKey: OWNER, body });
      assert.equal(result.ok, false, `expected rejection: ${url}`);
      if (!result.ok) assert.equal(result.status, 400);
    }
  });

  it("refuses a Spotify job when this machine has the source switched off", async () => {
    // The worker's kill switch defaults off (Spotify needs a Premium login), and
    // it refuses every Spotify job while it is off. Accepting the job here would
    // queue work that dies on claim with nothing for the user to act on.
    delete process.env.STEMIFY_SPOTIFY_ENABLED;
    const key = "client-key-spotify-off-0001";
    try {
      const result = await createJob({
        ownerKey: OWNER,
        body: {
          ...structuredClone(validBody),
          source: { type: "spotify", url: "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" },
          idempotencyKey: key,
        },
      });
      assert.equal(result.ok, false);
      if (!result.ok) {
        // Distinct from unsupported_source: the link is valid, the machine is
        // not set up, and the two need different instructions.
        assert.equal(result.error, "spotify_unavailable");
        assert.equal(result.status, 400);
      }
      // Nothing was queued for the worker to pick up and refuse.
      const row = db.get<{ n: number }>(
        "SELECT COUNT(*) AS n FROM jobs WHERE owner_key = ? AND idempotency_key_hash = ?",
        OWNER,
        idempotencyHash(OWNER, key),
      );
      assert.equal(row?.n, 0);
    } finally {
      process.env.STEMIFY_SPOTIFY_ENABLED = "1";
    }
  });

  it("refuses a YouTube link when this machine has switched YouTube off", async () => {
    // The worker's STEMIFY_YOUTUBE_ENABLED kill switch is mirrored here (and in
    // the picker): without this, the switch did nothing until the worker
    // refused every job with the generic download message.
    process.env.STEMIFY_YOUTUBE_ENABLED = "0";
    const key = "client-key-youtube-off-0001";
    try {
      const result = await createJob({
        ownerKey: OWNER,
        body: {
          ...structuredClone(validBody),
          source: { type: "youtube", url: "https://www.youtube.com/watch?v=abc123" },
          idempotencyKey: key,
        },
      });
      assert.equal(result.ok, false);
      if (!result.ok) {
        // Distinct from unsupported_source, exactly like Spotify above: the link
        // is valid, the machine is switched off, and the fixes differ.
        assert.equal(result.error, "youtube_unavailable");
        assert.equal(result.status, 400);
      }
      const row = db.get<{ n: number }>(
        "SELECT COUNT(*) AS n FROM jobs WHERE owner_key = ? AND idempotency_key_hash = ?",
        OWNER,
        idempotencyHash(OWNER, key),
      );
      assert.equal(row?.n, 0);
    } finally {
      process.env.STEMIFY_YOUTUBE_ENABLED = "1";
    }
  });

  it("accepts a YouTube link when the switch is on, as it is by default", async () => {
    delete process.env.STEMIFY_YOUTUBE_ENABLED;
    try {
      const result = await createJob({
        ownerKey: OWNER,
        body: {
          ...structuredClone(validBody),
          source: { type: "youtube", url: "https://youtu.be/abc123" },
          idempotencyKey: "client-key-youtube-default-01",
        },
      });
      assert.equal(result.ok, true);
    } finally {
      process.env.STEMIFY_YOUTUBE_ENABLED = "1";
    }
  });

  it("rejects an unknown source type", async () => {
    const body = {
      ...structuredClone(validBody),
      source: { type: "tidal", url: "https://tidal.com/track/1" },
      idempotencyKey: "client-key-unknown-source-01",
    };
    const result = await createJob({ ownerKey: OWNER, body });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });

  it("persists the chosen quality preset on the created job", async () => {
    const body = {
      ...structuredClone(validBody),
      quality: "balanced",
      idempotencyKey: "client-key-quality-0000001",
    } as typeof validBody & { quality: string };
    const result = await createJob({ ownerKey: OWNER, body });
    assert.equal(result.ok, true);
    if (!result.ok) return;
    const row = db.get<{ quality: string | null }>(
      "SELECT quality FROM jobs WHERE id = ?",
      result.job.id,
    );
    assert.equal(row?.quality, "balanced");
  });

  it("rejects an unknown quality preset", async () => {
    const bad = {
      ...structuredClone(validBody),
      quality: "ultra",
      idempotencyKey: "client-key-quality-0000002",
    } as typeof validBody & { quality: string };
    const result = await createJob({ ownerKey: OWNER, body: bad });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });

  it("stores null quality when the picker value is omitted (worker default)", async () => {
    const body = {
      ...structuredClone(validBody),
      idempotencyKey: "client-key-quality-0000003",
    };
    delete (body as { quality?: string }).quality;
    const result = await createJob({ ownerKey: OWNER, body });
    assert.equal(result.ok, true);
    if (!result.ok) return;
    const row = db.get<{ quality: string | null }>(
      "SELECT quality FROM jobs WHERE id = ?",
      result.job.id,
    );
    assert.equal(row?.quality, null);
  });
});
