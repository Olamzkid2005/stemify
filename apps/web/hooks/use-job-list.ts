"use client";

/**
 * Polls GET /api/jobs (concurrency plan C4): one poller per page.
 *
 * Faster while something is moving (2.5 s) and slow when everything is idle
 * (15 s), because a queue only changes when a worker acts. Pauses while the tab
 * is hidden and resumes on return — a background tab must not keep asking.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  isActiveStatus,
  isPoolInfo,
  type JobListItem,
  type JobListResponse,
  type WorkerPoolInfo,
} from "@/lib/job-list-types";

const ACTIVE_INTERVAL_MS = 2500;
const IDLE_INTERVAL_MS = 15_000;

export type JobListState = {
  jobs: JobListItem[];
  activeCount: number;
  /**
   * Machine summary from the same response, or null before the first successful
   * poll (the strip renders nothing rather than flashing "no worker").
   */
  pool: WorkerPoolInfo | null;
  /** True when the last refresh failed; the list keeps its last known rows. */
  error: boolean;
  loading: boolean;
  /**
   * Cancel a job (stop button in the list row). The response is deliberately
   * ignored — the next poll, triggered immediately, is the truth about the
   * state; a 409 means the job finished first and already shows its terminal
   * state.
   */
  cancelJob: (jobId: string) => void;
  /** The job a cancel was just requested for, so the row can say "Stopping…". */
  cancelingJobId: string | null;
};

export function useJobListPolling(): JobListState {
  const [state, setState] = useState<
    Omit<JobListState, "cancelJob"> & { cancelingJobId: string | null }
  >({ jobs: [], activeCount: 0, pool: null, error: false, loading: true, cancelingJobId: null });
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const delayRef = useRef(ACTIVE_INTERVAL_MS);
  // Ref, not state: pollNow must read a fresh value without re-creating the
  // callback the rows render.
  const pollNowRef = useRef<() => void>(() => {});

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
        // Validated, not trusted: the strip would otherwise render `undefined`
        // workers if the field ever went missing.
        const pool = isPoolInfo(body.pool) ? body.pool : null;
        delayRef.current = activeCount > 0 ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
        setState((prev) => ({ jobs, activeCount, pool, error: false, loading: false, cancelingJobId: prev.cancelingJobId }));
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
  }, []);

  const cancelJob = useCallback((jobId: string) => {
    setState((prev) => ({ ...prev, cancelingJobId: jobId }));
    void fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" })
      .catch(() => undefined)
      .finally(() => {
        setState((prev) => (prev.cancelingJobId === jobId ? { ...prev, cancelingJobId: null } : prev));
        pollNowRef.current();
      });
  }, []);

  return { ...state, cancelJob };
}
