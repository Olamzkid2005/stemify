/**
 * Seed-test helper (plan Task 3: "Add seed/test helpers").
 *
 * Inserts one fixture job with outputs against the real database, verifies
 * reads, the idempotency uniqueness constraint, and cascade delete, then
 * removes everything it created. Safe to run repeatedly.
 *
 * Run from apps/web:  node --run lib/db/seed-test.ts
 * (or: npx tsx lib/db/seed-test.ts)
 *
 * Requires DATABASE_URL in the environment (.env at the repo root).
 */
import { eq } from "drizzle-orm";
import { db, sql } from "./client";
import { jobOutputs, jobs } from "./schema";

const SEED_JOB_ID = "job_seedtest000000000001";

async function seedTest(): Promise<void> {
  // 1. Insert fixture job in a non-terminal state.
  await db.insert(jobs).values({
    id: SEED_JOB_ID,
    ownerKey: "seed-test-owner",
    sourceType: "upload",
    sourceFilename: "seed-test.mp3",
    sourceObjectKey: `sources/upl_seedtest000000000001/input.mp3`,
    mode: "vocals_instrumental",
    outputFormat: "mp3",
    status: "processing",
    stage: "separating",
    progress: 54,
    idempotencyKeyHash: "seedtest-idempotency-hash",
  });
  console.log("inserted job", SEED_JOB_ID);

  // 2. Insert outputs.
  await db.insert(jobOutputs).values([
    {
      jobId: SEED_JOB_ID,
      stemKey: "vocals",
      label: "Vocals",
      objectKey: `results/${SEED_JOB_ID}/vocals.mp3`,
      mimeType: "audio/mpeg",
      sizeBytes: 3_400_000,
      durationSeconds: 214.2,
    },
    {
      jobId: SEED_JOB_ID,
      stemKey: "instrumental",
      label: "Instrumental",
      objectKey: `results/${SEED_JOB_ID}/instrumental.mp3`,
      mimeType: "audio/mpeg",
      sizeBytes: 3_100_000,
      durationSeconds: 214.2,
    },
  ]);
  console.log("inserted 2 outputs");

  // 3. Read back and verify.
  const [job] = await db.select().from(jobs).where(eq(jobs.id, SEED_JOB_ID));
  const outputs = await db
    .select()
    .from(jobOutputs)
    .where(eq(jobOutputs.jobId, SEED_JOB_ID));
  if (job.status !== "processing" || job.progress !== 54) {
    throw new Error(`job roundtrip mismatch: ${JSON.stringify(job)}`);
  }
  if (outputs.length !== 2) {
    throw new Error(`expected 2 outputs, got ${outputs.length}`);
  }
  console.log("read back: job + 2 outputs OK");

  // 4. Idempotency constraint: a second job with the same owner + key must fail.
  let constraintHit = false;
  try {
    await db.insert(jobs).values({
      id: "job_seedtest000000000002",
      ownerKey: "seed-test-owner",
      sourceType: "upload",
      sourceFilename: "seed-test-2.mp3",
      mode: "vocals_instrumental",
      outputFormat: "mp3",
      idempotencyKeyHash: "seedtest-idempotency-hash",
    });
  } catch {
    constraintHit = true;
    console.log("idempotency unique constraint enforced OK");
  }
  if (!constraintHit) {
    throw new Error("duplicate idempotency key was accepted — constraint broken");
  }

  // 5. Cleanup: deleting the job must cascade to outputs.
  await db.delete(jobs).where(eq(jobs.id, SEED_JOB_ID));
  const remaining = await db
    .select()
    .from(jobOutputs)
    .where(eq(jobOutputs.jobId, SEED_JOB_ID));
  if (remaining.length !== 0) {
    throw new Error("cascade delete failed — orphaned outputs remain");
  }
  console.log("cascade delete OK");

  console.log("\nSEED TEST PASS: schema roundtrip verified end to end");
}

seedTest()
  .catch((err) => {
    console.error("SEED TEST FAIL:", err.message);
    process.exitCode = 1;
  })
  .finally(() => sql.end());
