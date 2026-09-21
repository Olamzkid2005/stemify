import assert from "node:assert/strict";
import { describe, it } from "node:test";

/**
 * Capability mirror tests (Spotify plan S2/S4, plan Task 14).
 *
 * These flags decide what the picker offers and what the job service accepts, so
 * they must agree with the worker's own kill switches exactly — including their
 * *opposite* defaults, which is the part that is easy to get wrong:
 *
 *   youtube  -> worker defaults ON  (`not in {0,false,no}`)
 *   spotify  -> worker defaults OFF (`in {1,true,yes}`)
 *
 * `process.env` is read per call, so each case sets and restores the variable.
 */
import { spotifyEnabled, youtubeEnabled } from "@/lib/capabilities";

function withEnv(name: string, value: string | undefined, run: () => void): void {
  const previous = process.env[name];
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
  try {
    run();
  } finally {
    if (previous === undefined) delete process.env[name];
    else process.env[name] = previous;
  }
}

describe("youtubeEnabled", () => {
  it("is on unless the operator explicitly switches it off", () => {
    withEnv("STEMIFY_YOUTUBE_ENABLED", undefined, () => {
      assert.equal(youtubeEnabled(), true);
    });
  });

  it("is on for the values .env.example ships and for blank", () => {
    for (const value of ["1", "true", "yes", "", "  ", "on"]) {
      withEnv("STEMIFY_YOUTUBE_ENABLED", value, () => {
        assert.equal(youtubeEnabled(), true, `value ${JSON.stringify(value)}`);
      });
    }
  });

  it("is off for exactly the off spellings the worker accepts", () => {
    // worker/worker/youtube.py: `not in {"0", "false", "no"}`. A value the
    // worker treats as "on" must never be read as "off" here, or the UI would
    // hide a source that works.
    for (const value of ["0", "false", "no", "FALSE", " No "]) {
      withEnv("STEMIFY_YOUTUBE_ENABLED", value, () => {
        assert.equal(youtubeEnabled(), false, `value ${JSON.stringify(value)}`);
      });
    }
  });
});

describe("spotifyEnabled", () => {
  it("is off until the operator opts in", () => {
    withEnv("STEMIFY_SPOTIFY_ENABLED", undefined, () => {
      assert.equal(spotifyEnabled(), false);
    });
  });

  it("is on only for the opt-in spellings (not for blank or 0)", () => {
    for (const value of ["1", "true", "yes", "TRUE", " Yes "]) {
      withEnv("STEMIFY_SPOTIFY_ENABLED", value, () => {
        assert.equal(spotifyEnabled(), true, `value ${JSON.stringify(value)}`);
      });
    }
    for (const value of ["0", "false", "no", ""]) {
      withEnv("STEMIFY_SPOTIFY_ENABLED", value, () => {
        assert.equal(spotifyEnabled(), false, `value ${JSON.stringify(value)}`);
      });
    }
  });

  it("defaults the opposite way to YouTube, as the worker does", () => {
    withEnv("STEMIFY_YOUTUBE_ENABLED", undefined, () => {
      withEnv("STEMIFY_SPOTIFY_ENABLED", undefined, () => {
        assert.equal(youtubeEnabled(), true);
        assert.equal(spotifyEnabled(), false);
      });
    });
  });
});
