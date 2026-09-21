import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

/**
 * First import: installs the jsdom globals that `react-dom/client` needs. Order
 * matters — imports are evaluated in document order, so this must precede the
 * React imports below.
 */
import "@/lib/test-dom";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { SourcePickerForm } from "@/components/source-picker";

/**
 * Source picker interaction tests (node:test + jsdom, no browser).
 *
 * These cover the published-tab behaviour, not the pixel output: which tabs
 * exist, what each one renders, and — the part that was actually broken — what
 * happens to the link and the policy acknowledgement when the source changes.
 * A source-specific acknowledgement must not survive a switch, or a YouTube
 * consent could submit a Spotify job.
 *
 * `SourcePickerForm` is the presentational half of the picker: the exported
 * `SourcePicker` wrapper only supplies the Next router, which cannot exist
 * outside an app-router context, so the form takes `navigate` as a prop.
 */

const TRACK_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT";
const ALBUM_URL = "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT";

type FetchCall = { url: string; init: RequestInit };

let containers: HTMLElement[];
let roots: Root[];
let fetchCalls: FetchCall[];
let navigated: string[];
let originalFetch: typeof fetch;

beforeEach(() => {
  containers = [];
  roots = [];
  fetchCalls = [];
  navigated = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    fetchCalls.push({ url: String(input), init: init ?? {} });
    return Promise.resolve({
      ok: true,
      status: 201,
      json: async () => ({ jobId: "job_abc123" }),
    } as unknown as Response);
  }) as typeof fetch;
});

afterEach(() => {
  act(() => {
    for (const root of roots) root.unmount();
  });
  for (const container of containers) container.remove();
  globalThis.fetch = originalFetch;
});

function render({
  spotifyAvailable = true,
  youtubeAvailable = true,
  activeJobs = 0,
}: {
  spotifyAvailable?: boolean;
  youtubeAvailable?: boolean;
  activeJobs?: number;
} = {}): HTMLElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  containers.push(container);
  roots.push(root);
  act(() => {
    // The real navigate pushes to the Next router; recording the href is what
    // the assertions need, and it keeps the router out of the test entirely.
    root.render(
      <SourcePickerForm
        activeJobs={activeJobs}
        navigate={(href) => navigated.push(href)}
        spotifyAvailable={spotifyAvailable}
        youtubeAvailable={youtubeAvailable}
      />,
    );
  });
  return container;
}

function tab(container: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from(container.querySelectorAll("button")).find((button) =>
    button.textContent?.includes(label),
  );
  assert.ok(found, `no tab button containing ${JSON.stringify(label)}`);
  return found as HTMLButtonElement;
}

function linkInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>("#link-url");
  assert.ok(input, "the link field is not rendered");
  return input;
}

function acknowledgement(container: HTMLElement): HTMLInputElement {
  const checkbox = container.querySelector<HTMLInputElement>("input[type=checkbox]");
  assert.ok(checkbox, "the acknowledgement checkbox is not rendered");
  return checkbox;
}

function submitButton(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button[type=submit]");
  assert.ok(button, "the submit button is not rendered");
  return button;
}

function click(element: Element): void {
  act(() => {
    (element as HTMLElement).click();
  });
}

/**
 * Type into a controlled input. React remembers the last value it rendered, so a
 * bare `input.value = x` is discarded; going through the native setter and firing
 * `input` is what the browser does when a person types.
 */
function type(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  assert.ok(setter, "HTMLInputElement has no native value setter");
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}

/** Submit the form the way the browser does, so `onSubmit` runs. */
function fireSubmit(container: HTMLElement): void {
  const form = container.querySelector("form");
  assert.ok(form, "the link form is not rendered");
  form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
}

/** Flush the fetch promise chain that a submit kicks off. */
async function flushPromises(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("SourcePickerForm", () => {
  it("renders all three source tabs with upload selected", () => {
    const container = render();

    for (const label of ["Upload File", "YouTube Link", "Spotify Link"]) {
      assert.ok(tab(container, label), `expected a ${label} tab`);
    }
    assert.equal(tab(container, "Upload File").getAttribute("aria-pressed"), "true");
    assert.equal(tab(container, "Spotify Link").getAttribute("aria-pressed"), "false");
    // Upload stays the default view: the dropzone, never the link form.
    assert.ok(container.textContent?.includes("Drop an audio file here"));
    assert.equal(container.querySelector("form"), null);
  });

  it("gives Spotify its own field and its own policy wording", () => {
    const container = render();
    click(tab(container, "Spotify Link"));

    assert.equal(tab(container, "Spotify Link").getAttribute("aria-pressed"), "true");
    assert.equal(tab(container, "Upload File").getAttribute("aria-pressed"), "false");
    assert.ok(container.textContent?.includes("Spotify track link"));
    assert.equal(linkInput(container).placeholder, "https://open.spotify.com/track/…");
    // Consent is per-source, so the acknowledgement must name the right service.
    assert.ok(container.textContent?.includes("Spotify's Terms of Service"));
    assert.ok(container.textContent?.includes("my own Premium account"));
    assert.ok(!container.textContent?.includes("YouTube's Terms of Service"));
  });

  it("resets the link and the acknowledgement when the source changes", () => {
    const container = render();
    click(tab(container, "YouTube Link"));
    type(linkInput(container), "https://www.youtube.com/watch?v=abc123");
    click(acknowledgement(container));
    assert.equal(acknowledgement(container).checked, true);
    assert.equal(submitButton(container).disabled, false);

    click(tab(container, "Spotify Link"));

    // Regression: the acknowledgement used to survive the switch, so a YouTube
    // consent could submit a Spotify job, and the pasted link carried over too.
    assert.equal(acknowledgement(container).checked, false);
    assert.equal(linkInput(container).value, "");
    assert.equal(submitButton(container).disabled, true);
    assert.ok(!container.textContent?.includes("YouTube's Terms of Service"));
  });

  it("keeps submit disabled until the link and the acknowledgement are both set", () => {
    const container = render();
    click(tab(container, "Spotify Link"));
    assert.equal(submitButton(container).disabled, true);

    type(linkInput(container), TRACK_URL);
    assert.equal(submitButton(container).disabled, true, "link alone is not consent");

    click(acknowledgement(container));
    assert.equal(submitButton(container).disabled, false);

    click(acknowledgement(container));
    assert.equal(submitButton(container).disabled, true);
  });

  it("explains why the submit button will not respond", () => {
    const container = render();
    click(tab(container, "Spotify Link"));

    // Regression: the button used to be silently disabled, so clicking it after
    // pasting a link did nothing at all — no message, no request, no hint.
    assert.equal(submitButton(container).disabled, true);
    assert.ok(container.textContent?.includes("Paste a link above to continue."));

    type(linkInput(container), TRACK_URL);
    assert.equal(submitButton(container).disabled, true);
    assert.ok(container.textContent?.includes("Tick the box above to confirm"));

    click(acknowledgement(container));
    assert.equal(submitButton(container).disabled, false);
    assert.equal(container.textContent?.includes("Tick the box above to confirm"), false);
  });

  it("names the missing acknowledgement when submit is reached anyway", async () => {
    const container = render();
    click(tab(container, "Spotify Link"));
    type(linkInput(container), TRACK_URL);
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.ok(container.textContent?.includes("Tick the box above to confirm"));
    assert.equal(fetchCalls.length, 0, "nothing is requested without consent");
    assert.deepEqual(navigated, []);
  });

  it("reports a stale server as unreachable instead of a rejected job", async () => {
    // What a rebuilt route answers: 404 with an HTML body, not JSON.
    globalThis.fetch = (() =>
      Promise.resolve({
        ok: false,
        status: 404,
        json: async () => {
          throw new Error("not json");
        },
      } as unknown as Response)) as typeof fetch;

    const container = render();
    click(tab(container, "Spotify Link"));
    type(linkInput(container), TRACK_URL);
    click(acknowledgement(container));
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.deepEqual(navigated, []);
    assert.ok(container.textContent?.includes("Could not reach the local service"));
    assert.ok(container.textContent?.includes("refresh this page"));
  });

  it("refuses a Spotify album link with the Spotify-specific message", () => {
    const container = render();
    click(tab(container, "Spotify Link"));
    type(linkInput(container), ALBUM_URL);
    click(acknowledgement(container));
    act(() => {
      fireSubmit(container);
    });

    assert.ok(container.textContent?.includes("albums and playlists are not supported yet"));
    // Nothing left the client: no job was created and nothing was navigated.
    assert.equal(fetchCalls.length, 0);
    assert.deepEqual(navigated, []);
  });

  it("posts the Spotify source and navigates to the created job", async () => {
    const container = render();
    click(tab(container, "Spotify Link"));
    type(linkInput(container), TRACK_URL);
    click(acknowledgement(container));
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.equal(fetchCalls.length, 1);
    assert.equal(fetchCalls[0].url, "/api/jobs");
    const body = JSON.parse(String(fetchCalls[0].init.body)) as {
      source: unknown;
      mode: string;
      outputFormat: string;
      quality: string;
      idempotencyKey: string;
    };
    assert.deepEqual(body.source, { type: "spotify", url: TRACK_URL });
    assert.equal(body.mode, "vocals_instrumental");
    assert.equal(body.outputFormat, "mp3");
    assert.equal(body.quality, "balanced");
    assert.match(body.idempotencyKey, /^[0-9a-f-]{36}$/);
    assert.deepEqual(navigated, ["/jobs/job_abc123"]);
    assert.equal(container.textContent?.includes("could not be started"), false);
  });

  it("accepts the spotify:track: URI the desktop app copies", async () => {
    const container = render();
    click(tab(container, "Spotify Link"));
    // Regression: the server and the worker both accept this form while the
    // client pattern did not, so a pasted URI was rejected before it could be
    // submitted.
    type(linkInput(container), "spotify:track:4cOdK2wGLETKBW3PvgPWqT");
    click(acknowledgement(container));
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.equal(fetchCalls.length, 1);
    assert.equal(fetchCalls[0].url, "/api/jobs");
    const body = JSON.parse(String(fetchCalls[0].init.body)) as { source: unknown };
    assert.deepEqual(body.source, { type: "spotify", url: "spotify:track:4cOdK2wGLETKBW3PvgPWqT" });
    assert.deepEqual(navigated, ["/jobs/job_abc123"]);
  });

  it("says why Spotify is unavailable instead of offering a form that can only fail", () => {
    const container = render({ spotifyAvailable: false });
    click(tab(container, "Spotify Link"));

    // No field and no form: a job created here would be refused on claim.
    assert.equal(container.querySelector("form"), null);
    assert.equal(container.querySelector("#link-url"), null);
    assert.ok(container.textContent?.includes("Spotify input is switched off on this machine"));
    assert.ok(container.textContent?.includes("STEMIFY_SPOTIFY_ENABLED=1"));

    // Other sources are unaffected by one source's capability.
    click(tab(container, "YouTube Link"));
    assert.ok(container.querySelector("#link-url"), "YouTube still takes a link");
    click(tab(container, "Upload File"));
    assert.ok(container.textContent?.includes("Drop an audio file here"));
  });

  it("says why YouTube is unavailable when the worker's switch is off", () => {
    // Mirrors the Spotify case above: STEMIFY_YOUTUBE_ENABLED=0 makes every
    // YouTube job fail on claim, so the form must not be offered at all.
    const container = render({ youtubeAvailable: false });
    click(tab(container, "YouTube Link"));

    assert.equal(container.querySelector("form"), null);
    assert.equal(container.querySelector("#link-url"), null);
    assert.ok(container.textContent?.includes("YouTube input is switched off on this machine"));
    assert.ok(container.textContent?.includes("STEMIFY_YOUTUBE_ENABLED"));

    // The other sources keep working.
    click(tab(container, "Spotify Link"));
    assert.ok(container.querySelector("#link-url"), "Spotify still takes a link");
    click(tab(container, "Upload File"));
    assert.ok(container.textContent?.includes("Drop an audio file here"));
  });

  it("warns that another job already shares the CPU, and still accepts", async () => {
    const container = render({ activeJobs: 2 });

    // Concurrency plan 3.6: warn, never refuse — submitting while others run is
    // a legitimate thing to do, it just makes every job slower.
    assert.ok(container.textContent?.includes("You already have 2 jobs running"));
    assert.ok(container.textContent?.includes("shares the same CPU"));

    click(tab(container, "YouTube Link"));
    type(linkInput(container), "https://www.youtube.com/watch?v=abc123");
    click(acknowledgement(container));
    assert.equal(submitButton(container).disabled, false, "the warning must not block submission");
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.equal(fetchCalls.length, 1);
    assert.deepEqual(navigated, ["/jobs/job_abc123"]);
  });

  it("says nothing about other jobs when there are none", () => {
    const container = render();
    assert.equal(container.textContent?.includes("already have"), false);
  });

  it("surfaces a rejected job instead of navigating", async () => {
    globalThis.fetch = (() =>
      Promise.resolve({
        ok: false,
        status: 400,
        json: async () => ({ error: "unsupported_source" }),
      } as unknown as Response)) as typeof fetch;

    const container = render();
    click(tab(container, "Spotify Link"));
    type(linkInput(container), TRACK_URL);
    click(acknowledgement(container));
    await act(async () => {
      fireSubmit(container);
    });
    await flushPromises();

    assert.deepEqual(navigated, []);
    assert.ok(container.textContent?.includes("open.spotify.com/track/…"));
  });
});
