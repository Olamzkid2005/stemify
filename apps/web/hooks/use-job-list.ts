"use client";

/**
 * Polls GET /api/jobs (concurrency plan C4): one poller per page.
 *
 * Faster while something is moving (2.5 s) and slow when everything is idle
 * (15 s), because a queue only changes when a worker acts. Pauses while the tab
 * is hidden and resumes on return — a background tab must not keep asking.
 */
import { useEffect, useRef, useState } from "react";

import { isActiveStatus, type JobListItem, type JobListResponse } from "@/lib/job-list-types";

const ACTIVE_INTERVAL_MS = 2500;
const IDLE_INTERVAL_MS = 15_000;

export type JobListState = {
  jobs: JobListItem[];
  activeCount: number;
  /** True when the last refresh failed; the list keeps its last known rows. */
  error: boolean;
  loading: boolean;
};

const EMPTY: JobListState = { jobs: [], activeCount: 0, error: false, loading: true };

export function useJobListPolling(): JobListState {
  const [state, setState] = useState<JobListState>(EMPTY);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const delayRef = useRef(ACTIVE_INTERVAL_MS);

  useEffect(() => {
    stoppedRef.current = false;

    async function tick() {
      if (stoppedRef.current) return;
      try {
        const res = await fetch("/api/jobs", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as JobListResponse;
        const jobs = Array.isArray(body.jobs) ? body.jobs : [];
        const activeCount = jobs.filter((job) => isActiveStatus(job.status)).length;
        delayRef.current = activeCount > 0 ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
        setState({ jobs, activeCount, error: false, loading: false });
      } catch {
        // Keep whatever was rendered: a failed refresh is not an empty list.
        setState((prev) => ({ ...prev, error: true, loading: false }));
        delayRef.current = IDLE_INTERVAL_MS;
      }
      schedule();
    }

    function schedule() {
      if (stoppedRef.current) return;
      if (typeof document !== "undefined" && document.hidden) {
        document.addEventListener("visibilitychange", onVisible, { once: true });
        return;
      }
      timerRef.current = setTimeout(tick, delayRef.current);
    }

    function onVisible() {
      schedule();
    }

    void tick();
    return () => {
      stoppedRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  return state;
}
