/**
 * Database schema — Stemify job metadata (plan Section 12).
 *
 * The database stores metadata and state, never audio bytes. Column values for
 * status/stage/mode/format/error codes mirror packages/contracts/schemas.
 */
import { relations, isNotNull } from "drizzle-orm";
import {
  bigint,
  doublePrecision,
  index,
  integer,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

export const jobs = pgTable(
  "jobs",
  {
    id: text("id").primaryKey(), // public app_job_id, "job_..."
    accessTokenHash: text("access_token_hash"),
    ownerKey: text("owner_key"), // guest cookie id now; account id later
    sourceType: text("source_type").$type<"upload" | "youtube">().notNull(),
    sourceFilename: text("source_filename"),
    sourceObjectKey: text("source_object_key"),
    sourceUrl: text("source_url"),
    sourceDurationSeconds: doublePrecision("source_duration_seconds"),
    sourceSizeBytes: bigint("source_size_bytes", { mode: "number" }),
    sourceSha256: text("source_sha256"),
    mode: text("mode").$type<"vocals_instrumental" | "full_stems">().notNull(),
    outputFormat: text("output_format")
      .$type<"mp3" | "wav" | "flac" | "ogg" | "m4a">()
      .notNull(),
    status: text("status")
      .$type<
        | "queued"
        | "processing"
        | "completed"
        | "failed"
        | "canceled"
        | "expired"
      >()
      .notNull()
      .default("queued"),
    stage: text("stage"),
    progress: integer("progress").notNull().default(0),
    workerCallId: text("worker_call_id"), // internal, never exposed
    idempotencyKeyHash: text("idempotency_key_hash"),
    errorCode: text("error_code"),
    errorMessagePublic: text("error_message_public"),
    diagnosticReference: text("diagnostic_reference"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    startedAt: timestamp("started_at", { withTimezone: true }),
    completedAt: timestamp("completed_at", { withTimezone: true }),
    expiresAt: timestamp("expires_at", { withTimezone: true }),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (table) => [
    index("jobs_status_created_idx").on(table.status, table.createdAt),
    index("jobs_expires_at_idx").on(table.expiresAt),
    index("jobs_worker_call_id_idx").on(table.workerCallId),
    uniqueIndex("jobs_owner_idempotency_idx")
      .on(table.ownerKey, table.idempotencyKeyHash)
      .where(isNotNull(table.idempotencyKeyHash)),
  ],
).enableRLS();

export const jobOutputs = pgTable(
  "job_outputs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    jobId: text("job_id")
      .notNull()
      .references(() => jobs.id, { onDelete: "cascade" }),
    stemKey: text("stem_key").notNull(), // vocals | instrumental | drums | bass | other
    label: text("label").notNull(),
    objectKey: text("object_key").notNull(),
    mimeType: text("mime_type").notNull(),
    sizeBytes: bigint("size_bytes", { mode: "number" }),
    durationSeconds: doublePrecision("duration_seconds"),
    sha256: text("sha256"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    expiresAt: timestamp("expires_at", { withTimezone: true }),
  },
  (table) => [index("job_outputs_job_id_idx").on(table.jobId)],
).enableRLS();

export const workerEvents = pgTable("worker_events", {
  id: uuid("id").primaryKey().defaultRandom(),
  eventId: text("event_id").notNull().unique(), // idempotency for callbacks
  jobId: text("job_id")
    .notNull()
    .references(() => jobs.id, { onDelete: "cascade" }),
  eventType: text("event_type").notNull(),
  payloadHash: text("payload_hash"),
  receivedAt: timestamp("received_at", { withTimezone: true }).notNull().defaultNow(),
}, (table) => [index("worker_events_job_id_idx").on(table.jobId)]).enableRLS();

export const usageEvents = pgTable(
  "usage_events",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    jobId: text("job_id").references(() => jobs.id, { onDelete: "set null" }),
    actorKeyHash: text("actor_key_hash"), // hashed IP/session, never raw IPs
    eventType: text("event_type").notNull(),
    mode: text("mode"),
    durationSeconds: doublePrecision("duration_seconds"),
    gpuSeconds: doublePrecision("gpu_seconds"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (table) => [
    index("usage_events_actor_idx").on(table.actorKeyHash, table.createdAt),
    index("usage_events_job_id_idx").on(table.jobId),
  ],
).enableRLS();

export const jobsRelations = relations(jobs, ({ many }) => ({
  outputs: many(jobOutputs),
  events: many(workerEvents),
}));

export const jobOutputsRelations = relations(jobOutputs, ({ one }) => ({
  job: one(jobs, { fields: [jobOutputs.jobId], references: [jobs.id] }),
}));
