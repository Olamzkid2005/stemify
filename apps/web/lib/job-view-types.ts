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
  // Roadmap Phase C: from the manifest inside the worker-built archive;
  // absent when analysis was unavailable.
  analysis?: { bpm: number; key: string; camelot: string };
  // failed only:
  errorCode?: string;
  errorMessage?: string | null;
  // Task 13: false only while a queued/processing job sees a stale worker heartbeat.
  workerRunning?: boolean;
};

const TERMINAL = new Set(["completed", "failed", "canceled", "expired"]);

export function isTerminalStatus(status: string): boolean {
  return TERMINAL.has(status);
}
