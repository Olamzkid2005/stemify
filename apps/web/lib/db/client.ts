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

import { SQLITE_SCHEMA } from "./schema";

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
    this.connection.exec(SQLITE_SCHEMA);
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
