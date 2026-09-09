/**
 * Test bootstrap: MUST be the first import in any test file that touches
 * `@/lib/db/client` (directly or transitively).
 *
 * The shared `db` singleton opens the SQLite file at module-load time, and
 * ESM/TS imports hoist above module bodies — so setting STEMIFY_DATA_DIR in
 * a test file's body is too late. Static imports execute in document order,
 * which makes a first-line import of this module the reliable hook: it sets
 * the data directory before anything from @/lib evaluates.
 *
 * Each runner process gets one fresh temp directory, so test runs never read
 * or mutate the developer's real app database.
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

process.env.STEMIFY_DATA_DIR ??= mkdtempSync(path.join(tmpdir(), "stemify-web-tests-"));
process.env.JOB_ACCESS_TOKEN_SECRET ??= "test-secret-for-local-tests-only";
