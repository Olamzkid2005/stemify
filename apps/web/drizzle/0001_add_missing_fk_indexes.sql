CREATE INDEX "usage_events_job_id_idx" ON "usage_events" USING btree ("job_id");--> statement-breakpoint
CREATE INDEX "worker_events_job_id_idx" ON "worker_events" USING btree ("job_id");