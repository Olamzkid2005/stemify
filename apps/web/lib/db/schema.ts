/**
 * Local SQLite row types and schema names.
 *
 * SQLite is the shared local queue database used by the Next.js process and the
 * Python worker. Audio bytes remain in the filesystem; this module only models
 * metadata and state.
 */

export type JobStatus = "queued" | "processing" | "completed" | "failed" | "canceled" | "expired";
export type SourceType = "upload" | "youtube";
export type SeparationMode = "vocals_instrumental" | "full_stems";
export type OutputFormat = "mp3" | "wav" | "flac" | "ogg" | "m4a";

export type JobRow = {
  id: string;
  access_token_hash: string | null;
  owner_key: string;
  source_type: SourceType;
  source_filename: string | null;
  source_object_key: string | null;
  source_path: string | null;
  source_url: string | null;
  source_duration_seconds: number | null;
  source_size_bytes: number | null;
  source_sha256: string | null;
  mode: SeparationMode;
  output_format: OutputFormat;
  status: JobStatus;
  stage: string | null;
  progress: number;
  cancel_requested: number;
  worker_call_id: string | null;
  idempotency_key_hash: string | null;
  error_code: string | null;
  error_message_public: string | null;
  diagnostic_reference: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  expires_at: number | null;
  updated_at: number;
};

export type UploadRow = {
  id: string;
  owner_key: string;
  filename: string;
  object_key: string;
  size_bytes: number;
  created_at: number;
  expires_at: number | null;
};

export type JobOutputRow = {
  id: string;
  job_id: string;
  stem_key: string;
  label: string;
  relative_path: string;
  mime_type: string;
  size_bytes: number | null;
  duration_seconds: number | null;
  sha256: string | null;
  created_at: number;
  expires_at: number | null;
};

export const SQLITE_SCHEMA = `
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  owner_key TEXT NOT NULL,
  filename TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS uploads_owner_created_idx ON uploads(owner_key, created_at);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  access_token_hash TEXT,
  owner_key TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('upload', 'youtube')),
  source_filename TEXT,
  source_object_key TEXT,
  source_path TEXT,
  source_url TEXT,
  source_duration_seconds REAL,
  source_size_bytes INTEGER,
  source_sha256 TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('vocals_instrumental', 'full_stems')),
  output_format TEXT NOT NULL CHECK (output_format IN ('mp3', 'wav', 'flac', 'ogg', 'm4a')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'canceled', 'expired')),
  stage TEXT,
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
  worker_call_id TEXT,
  idempotency_key_hash TEXT,
  error_code TEXT,
  error_message_public TEXT,
  diagnostic_reference TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  started_at INTEGER,
  completed_at INTEGER,
  expires_at INTEGER,
  updated_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  UNIQUE (owner_key, idempotency_key_hash)
);

CREATE TABLE IF NOT EXISTS job_outputs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stem_key TEXT NOT NULL,
  label TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER,
  duration_seconds REAL,
  sha256 TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS job_events (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  stage TEXT,
  progress INTEGER CHECK (progress BETWEEN 0 AND 100),
  detail TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000)
);

CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_expires_at_idx ON jobs(expires_at);
CREATE INDEX IF NOT EXISTS job_outputs_job_id_idx ON job_outputs(job_id);
CREATE INDEX IF NOT EXISTS job_events_job_id_idx ON job_events(job_id);
`;
