import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";

/**
 * Richer job metadata: the album the worker resolved and the cover it saved.
 *
 * Local-first by contract — the view must expose a cover URL that points at
 * this app, never at Spotify's image CDN — and both fields stay out of the
 * response when they do not exist, so the job page simply shows less rather
 * than a broken image.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh temp
 * directory before the @/lib modules (and the `db` singleton) load.
 */
import "../lib/test-env";
import { findJobArtwork, jobArtworkUrl } from "@/lib/artwork";
import { closeDatabase, db } from "@/lib/db/client";
import { getJobView } from "@/lib/job-view";
import { __setStorageForTests } from "@/lib/storage";
import { LocalStorage } from "@/lib/storage/local";

const OWNER = "gid_artworkowner0000001";
const COVERED = `job_${"1".repeat(32)}`;
const PLAIN = `job_${"2".repeat(32)}`;
const MISMATCHED = `job_${"3".repeat(32)}`;

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 4]);

let storage: LocalStorage;
let tempDir: string;

function insertJob(
  id: string,
  ownerKey: string,
  album: string | null,
  filename: string | null = null,
): void {
  db.run(
    `INSERT OR REPLACE INTO jobs
       (id, owner_key, source_type, source_filename, source_album, mode, output_format, status)
     VALUES (?, ?, 'spotify', ?, ?, 'vocals_instrumental', 'mp3', 'processing')`,
    id,
    ownerKey,
    filename,
    album,
  );
}

describe("job album artwork", () => {
  before(async () => {
    tempDir = await mkdtemp(path.join(tmpdir(), "stemify-artwork-"));
    storage = new LocalStorage(tempDir);
    __setStorageForTests(storage);

    insertJob(COVERED, OWNER, "Owotabua", "Olamide - Owotabua.ogg");
    await storage.putObject(`results/${COVERED}/artwork.jpg`, JPEG);

    // An album whose cover never landed (the CDN fetch failed): the album is
    // still worth showing.
    insertJob(PLAIN, OWNER, "No Cover", "Someone - No Cover.ogg");

    // A cover stored for a different job: the path is derived from the id, so
    // this must not leak into the other job's view.
    insertJob(MISMATCHED, OWNER, null);
    await storage.putObject(`results/${PLAIN}/other.jpg`, JPEG);
  });

  after(async () => {
    closeDatabase();
    await rm(tempDir, { recursive: true, force: true });
  });

  it("finds the cover the worker saved for the job", async () => {
    const artwork = await findJobArtwork(COVERED);

    assert.ok(artwork, "the stored cover should be found");
    assert.equal(artwork.relativePath, `results/${COVERED}/artwork.jpg`);
    assert.equal(artwork.sizeBytes, JPEG.byteLength);
    assert.ok(artwork.filePath.includes("artwork.jpg"));
  });

  it("reports no cover rather than throwing when there is none", async () => {
    assert.equal(await findJobArtwork(PLAIN), null);
    assert.equal(await findJobArtwork(`job_${"f".repeat(32)}`), null);
  });

  it("refuses ids that are not server-generated job ids", async () => {
    // Path safety: the id is interpolated into a path, so anything else is
    // rejected before the filesystem is touched.
    for (const bad of ["", "..", "job_../artwork", "../../etc/passwd", `job_${"a".repeat(31)}`]) {
      assert.equal(await findJobArtwork(bad), null, `expected ${JSON.stringify(bad)} to be refused`);
    }
  });

  it("exposes the album and a local cover URL on the job view", async () => {
    const view = await getJobView(COVERED, OWNER);

    assert.ok(view);
    assert.equal(view.source.album, "Owotabua");
    // The point of the local-first choice: this app serves the image, so the
    // browser never contacts i.scdn.co.
    assert.equal(view.source.artworkUrl, `/api/jobs/${COVERED}/artwork`);
    assert.ok(!view.source.artworkUrl?.includes("scdn"));
    assert.equal(jobArtworkUrl(COVERED), `/api/jobs/${COVERED}/artwork`);
  });

  it("keeps the album when the cover is missing", async () => {
    const view = await getJobView(PLAIN, OWNER);

    assert.equal(view?.source.album, "No Cover");
    assert.equal(view?.source.artworkUrl, undefined);
  });

  it("omits both fields entirely when neither exists", async () => {
    const view = await getJobView(MISMATCHED, OWNER);

    assert.ok(view);
    assert.equal("album" in view.source, false);
    assert.equal("artworkUrl" in view.source, false);
  });

  it("is owner-scoped like every other job lookup", async () => {
    assert.equal(await getJobView(COVERED, "gid_someoneelse00000000"), null);
  });
});
