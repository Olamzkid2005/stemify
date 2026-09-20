/**
 * Machine capability flags (Spotify plan S2/S4).
 *
 * The worker owns link-source *policy* (the allowlist, plan Section 8) and
 * re-validates every job. It also owns a **kill switch** per optional source,
 * because a source that needs operator setup cannot be served everywhere. This
 * module mirrors only that kill switch, so the web app never offers a source
 * this machine has switched off — which is otherwise a job that is created and
 * then fails within milliseconds of being claimed.
 *
 * Deliberately dependency-free: the home page (a server component) and the job
 * service both read it, and neither may pull the database into a page's module
 * graph.
 */

/** Truthiness the worker's kill switches accept (`worker/worker/spotify.py`). */
function flagEnabled(name: string): boolean {
  const raw = (process.env[name] ?? "").trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes";
}

/**
 * Whether this machine can serve Spotify links.
 *
 * Default **off**, matching the worker: Spotify input needs a Web API app and a
 * Premium account, so an absent flag means "not set up here" rather than "on".
 * The credentials themselves are never visible to this process — the worker
 * checks for those when it runs the job.
 */
export function spotifyEnabled(): boolean {
  return flagEnabled("STEMIFY_SPOTIFY_ENABLED");
}
