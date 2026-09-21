"use client";

/**
 * One job-list poller for the whole home page (concurrency plan C4).
 *
 * Both consumers need the same answer at the same moment: the picker warns
 * that submitting again shares the CPU, and the list shows what is running and
 * wherever a job sits in the queue. Two independent pollers would double the
 * requests and could disagree, so the page has a single provider and both read
 * from it.
 */
import { createContext, useContext, type ReactNode } from "react";

import { useJobListPolling, type JobListState } from "@/hooks/use-job-list";

const JobsContext = createContext<JobListState>({
  jobs: [],
  activeCount: 0,
  error: false,
  loading: true,
});

export function JobsProvider({ children }: { children: ReactNode }) {
  const state = useJobListPolling();
  return <JobsContext.Provider value={state}>{children}</JobsContext.Provider>;
}

export function useJobs(): JobListState {
  return useContext(JobsContext);
}
