import { db } from "./client";

/** Initialize or upgrade the local SQLite database. */
export function migrateLocalDatabase(): void {
  // The schema is idempotently applied by LocalDatabase construction.
  db.exec("PRAGMA wal_checkpoint(PASSIVE);");
}

if (process.argv[1]?.endsWith("migrate.ts")) {
  migrateLocalDatabase();
  console.log(`Local database ready: ${db.databasePath}`);
}
