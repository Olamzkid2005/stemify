import assert from "node:assert/strict";
import { after, describe, it } from "node:test";

/**
 * Job list tests (concurrency plan C4): owner scoping, ordering, the queue
 * position, and the cap.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh temp
 * directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { closeDatabase, db } from "@/lib/db/client";
import { JOB_LIST_LIMIT, listJobViews, queuedPositions } from "@/lib/job-list";

const OWNER = "gid_listowner00000000001";
const OTHER = "gid_listowner00000000002";

/** Fixed base timestamp so ordering assertions never depend on wall-clock. */
const T0 = 1_760_000_000_000;

type Seed = {
  id: string;
  status: string;
  /** Offset in ms from T0. */
  at: number;
  owner?: string;
  mode?: string;
  progress?: number;
  stage?: string | null;
  filename?: string;
};

function jobId(char: string): string {
  return `job_${char.repeat(32)}`;
}

function seedJob({
  id,
  status,
  at,
  owner = OWNER,
  mode = "vocals_instrumental",
  progress = 0,
  stage = null,
  filename = "song.mp3",
}: Seed): void {
  db.run(
    `INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename,
       mode, output_format, status, stage, progress, created_at, updated_at)
     VALUES (?, ?, 'upload', ?, ?, ?, 'mp3', ?, ?, ?, ?, ?)`,
    id,
    owner,
    `sources/${id}/song.mp3`,
    filename,
    mode,
    status,
    stage,
    progress,
    T0 + at,
    T0 + at,
  );
}

function clearJobs(): void {
  db.run("DELETE FROM jobs WHERE owner_key IN (?, ?)", OWNER, OTHER);
}

after(() => {
  clearJobs();
  closeDatabase();
});

describe("job list", () => {
  it("shows only this browser's jobs", async () => {
    clearJobs();
    seedJob({ id: jobId("a"), status: "processing", at: 0, stage: "separating", progress: 40 });
    seedJob({ id: jobId("b"), status: "processing", at: 1, owner: OTHER, stage: "separating" });

    const jobs = await listJobViews(OWNER);
    assert.deepEqual(
      jobs.map((job) => job.jobId),
      [jobId("a")],
    );
  });

  it("puts active jobs first in queue order, then finished newest first", async () => {
    clearJobs();
    seedJob({ id: jobId("1"), status: "completed", at: 10 });
    seedJob({ id: jobId("2"), status: "queued", at: 30 });
    seedJob({ id: jobId("3"), status: "failed", at: 40 });
    seedJob({ id: jobId("4"), status: "processing", at: 20, stage: "encoding", progress: 80 });
    seedJob({ id: jobId("5"), status: "queued", at: 50 });

    const jobs = await listJobViews(OWNER);
    // Active first, oldest first: the running job, then the two waiting ones in
    // the order a worker would take them. Then the finished ones, newest first.
    assert.deepEqual(
      jobs.map((job) => job.jobId),
      [jobId("4"), jobId("2"), jobId("5"), jobId("3"), jobId("1")],
    );
  });

  it("numbers waiting jobs in the exact order the worker claims them", async () => {
    clearJobs();
    // Same created_at on purpose: the claim's own tiebreak is the id, and the
    // position must use it too or it would report the wrong order.
    seedJob({ id: jobId("b"), status: "queued", at: 0 });
    seedJob({ id: jobId("a"), status: "queued", at: 0 });
    seedJob({ id: jobId("c"), status: "queued", at: 1 });

    const positions = queuedPositions();
    assert.equal(positions.get(jobId("a")), 1); // lowest id wins the tie
    assert.equal(positions.get(jobId("b")), 2);
    assert.equal(positions.get(jobId("c")), 3);

    // The position is only honest while it matches the queue's own ordering.
    const next = db.get<{ id: string }>(
      "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1",
    );
    assert.equal(positions.get(next?.id ?? ""), 1);

    const jobs = await listJobViews(OWNER);
    assert.deepEqual(
      jobs.map((job) => [job.jobId, job.queuePosition]),
      [
        [jobId("a"), 1],
        [jobId("b"), 2],
        [jobId("c"), 3],
      ],
    );
  });

  it("renumbers the queue as jobs start, and never positions a running job", async () => {
    clearJobs();
    seedJob({ id: jobId("a"), status: "queued", at: 0 });
    seedJob({ id: jobId("b"), status: "queued", at: 1 });

    // Exactly what the worker does to the front of the queue.
    db.run(
      "UPDATE jobs SET status = 'processing', stage = 'starting', progress = 5 WHERE id = ?",
      jobId("a"),
    );

    const jobs = await listJobViews(OWNER);
    const byId = new Map(jobs.map((job) => [job.jobId, job]));
    assert.equal(byId.get(jobId("a"))?.queuePosition, undefined);
    assert.equal(byId.get(jobId("b"))?.queuePosition, 1);
  });

  it("reports how many jobs wait ahead of a queued job", async () => {
    clearJobs();
    seedJob({ id: jobId("a"), status: "processing", at: 0, stage: "separating" });
    seedJob({ id: jobId("b"), status: "queued", at: 1 });
    seedJob({ id: jobId("c"), status: "queued", at: 2 });

    const jobs = await listJobViews(OWNER);
    const waiting = jobs.filter((job) => job.queuePosition !== undefined);
    // The queue is global, but only *waiting* jobs are in it: the position is
    // 1 + the queued jobs created before it (plan 3.6), so the job at the front
    // is "next in line" even while something else is running.
    assert.deepEqual(
      waiting.map((job) => job.queuePosition),
      [1, 2],
    );
  });

  it("counts waiting jobs across owners, because one queue serves them all", async () => {
    clearJobs();
    seedJob({ id: jobId("a"), status: "queued", at: 0, owner: OTHER });
    seedJob({ id: jobId("b"), status: "queued", at: 1, owner: OWNER });

    const [job] = await listJobViews(OWNER);
    // Another browser's job is genuinely ahead in the one shared queue; saying
    // "next in line" here would be a lie.
    assert.equal(job.queuePosition, 2);
  });

  it("caps the list at the documented limit", async () => {
    clearJobs();
    for (let index = 0; index < JOB_LIST_LIMIT + 4; index += 1) {
      seedJob({
        id: jobId(index.toString(16).padStart(1, "0")),
        status: "completed",
        at: index,
      });
    }
    const jobs = await listJobViews(OWNER);
    assert.equal(jobs.length, JOB_LIST_LIMIT);
  });

  it("carries what the list row renders, and a status URL per job", async () => {
    clearJobs();
    seedJob({
      id: jobId("d"),
      status: "processing",
      at: 0,
      mode: "full_stems",
      stage: "separating",
      progress: 42,
      filename: "Artist - Song.mp3",
    });

    const [job] = await listJobViews(OWNER);
    assert.equal(job.status, "processing");
    assert.equal(job.mode, "full_stems");
    assert.equal(job.progress, 42);
    assert.equal(job.source.filename, "Artist - Song.mp3");
    assert.equal(job.statusUrl, `/api/jobs/${jobId("d")}`);
    assert.equal(job.source.artworkUrl, undefined);
    assert.ok(job.userStage.length > 0);
    assert.ok(job.progressMessage.length > 0);
  });
});
