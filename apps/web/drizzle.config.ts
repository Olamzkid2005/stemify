/**
 * Database migrations are initialized by apps/web/lib/db/client.ts.
 *
 * This file remains so existing tooling does not fail while the project uses
 * Node's built-in SQLite driver instead of Drizzle's hosted Postgres driver.
 */
const localDatabaseConfig = {
  dialect: "sqlite",
  dbCredentials: {
    url: process.env.STEMIFY_DATA_DIR ?? "./data",
  },
};

export default localDatabaseConfig;
