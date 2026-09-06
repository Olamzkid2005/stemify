/**
 * Client-safe job view types and stage mapping (plan §8.3/§11.3).
 * Split from job-view.ts so client components never import the DB.
 */

export type JobView = {
  jobId: string;
  status: string;
  stage: string;
  userStage: string;
  progress: number;
  source: { filename: string | null };
  mode: string;
  outputFormat: string;
  createdAt: string;
  updatedAt: string;
  // completed only:
  stems?: { id: string; label: string; durationSeconds: number | null }[];
  downloadUrl?: string;
  expiresAt?: string;
  // failed only:
  errorCode?: string;
  errorMessage?: string | null;
};

const TERMINAL = new Set(["completed", "failed", "canceled", "expired"]);

export function isTerminalStatus(status: string): boolean {
  return TERMINAL.has(status);
}
