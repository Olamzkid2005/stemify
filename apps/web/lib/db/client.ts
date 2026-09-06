/**
 * App-side database client (plan Section 6.1/6.3).
 *
 * Uses the postgres-js driver over Supabase's transaction pooler.
 * `prepare: false` is required: PgBouncer in transaction mode does not
 * support prepared statements. The client is cached on globalThis in
 * development so Next.js hot reloads do not exhaust connection slots.
 */
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

const globalForDb = globalThis as unknown as {
  postgresClient?: postgres.Sql;
};

function createClient(): postgres.Sql {
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString || connectionString.includes("[YOUR-PASSWORD]")) {
    throw new Error(
      "DATABASE_URL is missing or incomplete. Set it in .env (transaction pooler URL).",
    );
  }
  return postgres(connectionString, { prepare: false, max: 10 });
}

export const sql: postgres.Sql =
  globalForDb.postgresClient ?? createClient();

if (process.env.NODE_ENV !== "production") {
  globalForDb.postgresClient = sql;
}

export const db = drizzle(sql, { schema });

export type Db = typeof db;
