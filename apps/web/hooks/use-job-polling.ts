"use client";

/**
 * Job polling hook (plan §10.3): 2–3 s base interval, exponential backoff
 * after unchanged responses, stops at terminal states, pauses when the tab
 * is hidden and resumes on return.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { isTerminalStatus, type JobView } from "@/lib/job-view-types";

const BASE_INTERVAL_MS = 2500;
const MAX_INTERVAL_MS = 10_000;

type PollState = {
  job: JobView | null;
  error: "not_found" | "network" | null;
  loading: boolean;
};

export function useJobPolling(jobId: string): PollState & {
  /** POST the cancel request; the next poll reflects the new state. */
  cancel: () => Promise<void>;
  /** True between the button press and the request settling. */
  cancelPending: boolean;
} {
  const [state, setState] = useState<PollState>({
    job: null,
    error: null,
    loading: true,
  });
  const [cancelPending, setCancelPending] = useState(false);
  const backoffRef = useRef(1);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);

  // Poll once right after a cancel: the state change (canceled / stopping
  // message) should appear immediately, not up to one interval later.
  const pollNowRef = useRef<() => void>(() => {});

  useEffect(() => {
    stoppedRef.current = false;

    async function tick() {
      if (stoppedRef.current) return;
      try {
        const res = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
        if (res.status === 404) {
          setState({ job: null, error: "not_found", loading: false });
          return;
        }
        if (!res.ok) throw new Error(String(res.status));
        const job = (await res.json()) as JobView;
        setState({ job, error: null, loading: false });
        if (isTerminalStatus(job.status)) return; // stop polling
        backoffRef.current = 1; // healthy — reset backoff
      } catch {
        setState((prev) => ({ ...prev, error: "network", loading: false }));
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_INTERVAL_MS / BASE_INTERVAL_MS);
      }
      schedule();
    }

    function schedule() {
      if (stoppedRef.current) return;
      if (typeof document !== "undefined" && document.hidden) {
        // Pause while hidden; resume on visibility.
        document.addEventListener("visibilitychange", onVisible, { once: true });
        return;
      }
      timerRef.current = setTimeout(tick, BASE_INTERVAL_MS * backoffRef.current);
    }

    function onVisible() {
      schedule();
    }

    pollNowRef.current = () => {
      if (stoppedRef.current) return;
      if (timerRef.current) clearTimeout(timerRef.current);
      void tick();
    };

    void tick();
    return () => {
      stoppedRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [jobId]);

  const cancel = useCallback(async () => {
    setCancelPending(true);
    try {
      // The response body is deliberately ignored: the next poll is the truth
      // about the state, and it is triggered right away below. A 409 (already
      // finished between render and click) shows up there as the terminal
      // state it already is — no error UI for winning a race.
      await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch {
      // Poll continues; the button stays available while the job is active.
    } finally {
      setCancelPending(false);
      pollNowRef.current();
    }
  }, [jobId]);

  return { ...state, cancel, cancelPending };
}
