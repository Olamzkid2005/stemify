CREATE TABLE "job_outputs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"job_id" text NOT NULL,
	"stem_key" text NOT NULL,
	"label" text NOT NULL,
	"object_key" text NOT NULL,
	"mime_type" text NOT NULL,
	"size_bytes" bigint,
	"duration_seconds" double precision,
	"sha256" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"expires_at" timestamp with time zone
);
--> statement-breakpoint
ALTER TABLE "job_outputs" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "jobs" (
	"id" text PRIMARY KEY NOT NULL,
	"access_token_hash" text,
	"owner_key" text,
	"source_type" text NOT NULL,
	"source_filename" text,
	"source_object_key" text,
	"source_url" text,
	"source_duration_seconds" double precision,
	"source_size_bytes" bigint,
	"source_sha256" text,
	"mode" text NOT NULL,
	"output_format" text NOT NULL,
	"status" text DEFAULT 'queued' NOT NULL,
	"stage" text,
	"progress" integer DEFAULT 0 NOT NULL,
	"worker_call_id" text,
	"idempotency_key_hash" text,
	"error_code" text,
	"error_message_public" text,
	"diagnostic_reference" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"started_at" timestamp with time zone,
	"completed_at" timestamp with time zone,
	"expires_at" timestamp with time zone,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "jobs" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "usage_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"job_id" text,
	"actor_key_hash" text,
	"event_type" text NOT NULL,
	"mode" text,
	"duration_seconds" double precision,
	"gpu_seconds" double precision,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "usage_events" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
CREATE TABLE "worker_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"event_id" text NOT NULL,
	"job_id" text NOT NULL,
	"event_type" text NOT NULL,
	"payload_hash" text,
	"received_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "worker_events_event_id_unique" UNIQUE("event_id")
);
--> statement-breakpoint
ALTER TABLE "worker_events" ENABLE ROW LEVEL SECURITY;--> statement-breakpoint
ALTER TABLE "job_outputs" ADD CONSTRAINT "job_outputs_job_id_jobs_id_fk" FOREIGN KEY ("job_id") REFERENCES "public"."jobs"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "usage_events" ADD CONSTRAINT "usage_events_job_id_jobs_id_fk" FOREIGN KEY ("job_id") REFERENCES "public"."jobs"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "worker_events" ADD CONSTRAINT "worker_events_job_id_jobs_id_fk" FOREIGN KEY ("job_id") REFERENCES "public"."jobs"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "job_outputs_job_id_idx" ON "job_outputs" USING btree ("job_id");--> statement-breakpoint
CREATE INDEX "jobs_status_created_idx" ON "jobs" USING btree ("status","created_at");--> statement-breakpoint
CREATE INDEX "jobs_expires_at_idx" ON "jobs" USING btree ("expires_at");--> statement-breakpoint
CREATE INDEX "jobs_worker_call_id_idx" ON "jobs" USING btree ("worker_call_id");--> statement-breakpoint
CREATE UNIQUE INDEX "jobs_owner_idempotency_idx" ON "jobs" USING btree ("owner_key","idempotency_key_hash") WHERE "jobs"."idempotency_key_hash" is not null;--> statement-breakpoint
CREATE INDEX "usage_events_actor_idx" ON "usage_events" USING btree ("actor_key_hash","created_at");