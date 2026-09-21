"use client";

/**
 * One job-list poller for the whole home page (concurrency plan C4).
 *
 * Every consumer needs the same answer at the same moment: the picker warns
 * that submitting again shares the CPU, the list shows what is running and
 * wherever a job sits in the queue, and the pool strip reports how many workers
 * will pick those jobs up. Two independent pollers would double the requests
 * and could disagree, so the page has a single provider and all of them read
 * from it.
 */
import { createContext, useContext, type ReactNode } from "react";

import { useJobListPolling, type JobListState } from "@/hooks/use-job-list";

const JobsContext = createContext<JobListState>({
  jobs: [],
  activeCount: 0,
  pool: null,
  error: false,
  loading: true,
  cancelJob: () => {},
  cancelingJobId: null,
});

export function JobsProvider({ children }: { children: ReactNode }) {
  const state = useJobListPolling();
  return <JobsContext.Provider value={state}>{children}</JobsContext.Provider>;
}

export function useJobs(): JobListState {
  return useContext(JobsContext);
}
