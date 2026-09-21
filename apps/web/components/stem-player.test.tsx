import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

/** Installs the jsdom globals React's client renderer needs (must be first). */
import "@/lib/test-dom";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { StemPlayer } from "@/components/stem-player";

/**
 * Per-stem player (dark custom transport replacing the native audio bar):
 * engine state drives the render, the waveform doubles as the seek bar, and
 * the no-canvas / no-decode fallbacks show placeholders instead of fake data.
 */

const STEM = { id: "drums", label: "Extracted Drums", durationSeconds: 211 };

let containers: HTMLElement[] = [];
let roots: Root[] = [];
let originalFetch: typeof fetch;

/**
 * The waveform decode starts with a fetch of the stem. Stub it to reject
 * immediately: the test asserts the placeholder fallback, not network HTML,
 * and a relative URL under Node's fetch would otherwise attempt a real DNS
 * lookup against the jsdom origin.
 */
async function render() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  containers.push(container);
  roots.push(root);

  await act(async () => {
    root.render(<StemPlayer jobId="job_test" stem={STEM} />);
    await Promise.resolve();
  });
  // Flush the rejected decode so its state update lands inside act.
  await act(async () => {
    await Promise.resolve();
  });
  return container;
}

beforeEach(() => {
  containers = [];
  roots = [];
  originalFetch = globalThis.fetch;
  globalThis.fetch = () => Promise.reject(new TypeError("offline test"));
});

afterEach(() => {
  for (const root of roots) {
    act(() => root.unmount());
  }
  for (const node of containers) node.remove();
  containers = [];
  roots = [];
  globalThis.fetch = originalFetch;
});

describe("StemPlayer render and a11y", () => {
  it("renders the transport with an accessible play control and slider", async () => {
    const view = await render();

    const play = view.querySelector('button[aria-label="Play Extracted Drums"]');
    assert.ok(play, "a play button labelled with the stem name exists");

    const slider = view.querySelector('[role="slider"]');
    assert.ok(slider, "the waveform announces itself as a seek slider");
    assert.equal(slider?.getAttribute("aria-valuenow"), "0");
  });

  it("starts hidden audio as the engine and shows placeholder when canvas is missing", async () => {
    const view = await render();

    const audio = view.querySelector("audio");
    assert.ok(audio, "an audio element remains in the DOM as the engine");
    assert.equal(audio?.getAttribute("preload"), "none");

    // jsdom has no canvas: the lane must fall back to a placeholder, not crash.
    const slider = view.querySelector('[role="slider"]');
    assert.ok(slider, "seek lane still present without canvas");
    assert.ok(
      view.querySelector('[data-waveform="placeholder"]'),
      "placeholder bars render when canvas is unavailable",
    );
  });
});
