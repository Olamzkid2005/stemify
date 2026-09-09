/**
 * Local SQLite database client.
 *
 * The web process and the Python worker share this file. Initialization is lazy
 * at module load, requires no external service, and configures SQLite for the
 * short concurrent transactions used by the local job queue.
 */
import { mkdirSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { JOBS_TABLE_DDL, SQLITE_SCHEMA } from "./schema";

const globalForDb = globalThis as unknown as {
  stemifyDatabase?: LocalDatabase;
};

function dataDirectory(): string {
  return path.resolve(process.env.STEMIFY_DATA_DIR ?? path.join(process.cwd(), "data"));
}

export class LocalDatabase {
  readonly dataDir: string;
  readonly databasePath: string;
  private readonly connection: DatabaseSync;

  constructor(directory = dataDirectory()) {
    this.dataDir = directory;
    this.databasePath = path.join(directory, "stemify.sqlite3");
    mkdirSync(directory, { recursive: true });
    this.connection = new DatabaseSync(this.databasePath);
    this.connection.exec("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;");
    // Migration must precede the schema script: the CREATE INDEX statements
    // would otherwise attach to the old jobs table and be dropped with it
    // during the rebuild (mirrors worker/worker/database.py).
    this.migrateJobsModeCheck();
    this.connection.exec(SQLITE_SCHEMA);
  }

  /**
   * Rebuild the jobs table if it still has the 2-value mode CHECK (SQLite
   * cannot alter a CHECK, and CHECKs re-evaluate on every UPDATE). No-op when
   * the column already accepts drum_breakdown.
   */
  private migrateJobsModeCheck(): void {
    const row = this.connection
      .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'")
      .get() as { sql?: string } | undefined;
    if (!row?.sql || row.sql.includes("drum_breakdown")) return;
    // Rebuilding a table that other tables reference via foreign keys:
    // modern SQLite rewrites the REFERENCES clauses in job_outputs to point
    // at jobs_old on ANY ALTER TABLE ... RENAME (even with FK enforcement
    // off — verified empirically on sqlite 3.50), and the subsequent DROP
    // leaves them dangling ("no such table: main.jobs_old" on every later
    // insert). The documented opt-out is legacy_alter_table during the
    // rename; FK enforcement also goes off for the rebuild, per the
    // sqlite.org altertable procedure. Both are restored afterwards, and
    // PRAGMA foreign_key_check verifies the rebuilt schema.
    this.connection.exec("PRAGMA foreign_keys = OFF");
    this.connection.exec("PRAGMA legacy_alter_table = ON");
    try {
      this.connection.exec(
        `
        BEGIN IMMEDIATE;
        ALTER TABLE jobs RENAME TO jobs_old;
        ${JOBS_TABLE_DDL}
        INSERT INTO jobs (
          id, access_token_hash, owner_key, source_type, source_filename,
          source_object_key, source_path, source_url, source_duration_seconds,
          source_size_bytes, source_sha256, mode, output_format, status, stage,
          progress, cancel_requested, worker_call_id, idempotency_key_hash,
          error_code, error_message_public, diagnostic_reference, created_at,
          started_at, completed_at, expires_at, updated_at
        )
        SELECT
          id, access_token_hash, owner_key, source_type, source_filename,
          source_object_key, source_path, source_url, source_duration_seconds,
          source_size_bytes, source_sha256, mode, output_format, status, stage,
          progress, cancel_requested, worker_call_id, idempotency_key_hash,
          error_code, error_message_public, diagnostic_reference, created_at,
          started_at, completed_at, expires_at, updated_at
        FROM jobs_old;
        DROP TABLE jobs_old;
        COMMIT;
        `,
      );
    } finally {
      this.connection.exec("PRAGMA legacy_alter_table = OFF");
      this.connection.exec("PRAGMA foreign_keys = ON");
    }
    const violations = this.connection.prepare("PRAGMA foreign_key_check").all();
    if (violations.length > 0) {
      throw new Error(`foreign key violations after jobs rebuild: ${JSON.stringify(violations)}`);
    }
  }

  exec(sql: string): void {
    this.connection.exec(sql);
  }

  run(sql: string, ...params: unknown[]): { changes: number; lastInsertRowid: number } {
    const result = this.connection.prepare(sql).run(...params);
    return {
      changes: Number(result.changes),
      lastInsertRowid: Number(result.lastInsertRowid),
    };
  }

  get<T extends Record<string, unknown>>(sql: string, ...params: unknown[]): T | undefined {
    return this.connection.prepare(sql).get<T>(...params);
  }

  all<T extends Record<string, unknown>>(sql: string, ...params: unknown[]): T[] {
    return this.connection.prepare(sql).all<T>(...params);
  }

  transaction<T>(callback: () => T): T {
    this.connection.exec("BEGIN IMMEDIATE");
    try {
      const result = callback();
      this.connection.exec("COMMIT");
      return result;
    } catch (error) {
      this.connection.exec("ROLLBACK");
      throw error;
    }
  }

  close(): void {
    this.connection.close();
  }
}

export const db = globalForDb.stemifyDatabase ?? new LocalDatabase();

if (process.env.NODE_ENV !== "production") {
  globalForDb.stemifyDatabase = db;
}

/** Close the shared connection in scripts/tests that own the process lifetime. */
export function closeDatabase(): void {
  db.close();
  if (globalForDb.stemifyDatabase === db) delete globalForDb.stemifyDatabase;
}
