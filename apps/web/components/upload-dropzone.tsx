"use client";

/** Local upload dropzone with client-side validation and byte progress. */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  CLIENT_LIMITS,
  probeAudio,
  validateFileSelection,
  type FileValidationResult,
} from "@/lib/limits";

type UploadState =
  | { phase: "empty" }
  | { phase: "selected"; file: File; durationSeconds: number | null }
  | { phase: "uploading"; file: File; percent: number }
  | { phase: "uploaded"; filename: string; uploadId: string }
  | { phase: "error"; message: string };

type UploadedFile = { uploadId: string; filename: string };

function formatBytes(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}

export function UploadDropzone({
  onUploaded,
}: {
  onUploaded?: (result: UploadedFile) => void;
}) {
  const [state, setState] = useState<UploadState>({ phase: "empty" });
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const xhrRef = useRef<XMLHttpRequest | null>(null);

  useEffect(() => () => xhrRef.current?.abort(), []);

  const startUpload = useCallback(
    (file: File) => {
      setState({ phase: "uploading", file, percent: 0 });
      const xhr = new XMLHttpRequest();
      xhrRef.current = xhr;

      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100);
          setState((previous) =>
            previous.phase === "uploading" ? { ...previous, percent } : previous,
          );
        }
      };
      xhr.onload = () => {
        xhrRef.current = null;
        if (xhr.status < 200 || xhr.status >= 300) {
          setState({ phase: "error", message: "Upload failed. Try again." });
          return;
        }
        try {
          const body = JSON.parse(xhr.responseText) as { uploadId?: string; filename?: string };
          if (!body.uploadId || !body.filename) throw new Error("invalid upload response");
          setState({ phase: "uploaded", uploadId: body.uploadId, filename: body.filename });
          onUploaded?.({ uploadId: body.uploadId, filename: body.filename });
        } catch {
          setState({ phase: "error", message: "Upload response was invalid. Try again." });
        }
      };
      xhr.onerror = () => {
        xhrRef.current = null;
        setState({ phase: "error", message: "Upload failed. Check your connection and try again." });
      };
      xhr.onabort = () => {
        xhrRef.current = null;
        setState({ phase: "empty" });
      };

      const form = new FormData();
      form.append("file", file, file.name);
      xhr.open("POST", "/api/uploads");
      xhr.send(form);
    },
    [onUploaded],
  );

  const handleFile = useCallback(
    async (file: File) => {
      const validation: FileValidationResult = validateFileSelection(file);
      if (!validation.ok) {
        setState({
          phase: "error",
          message:
            validation.reason === "too-large"
              ? `That file is ${formatBytes(file.size)}. The limit is ${formatBytes(CLIENT_LIMITS.maxUploadBytes)}.`
              : "That file type is not supported. Use MP3, WAV, FLAC, OGG, or M4A.",
        });
        return;
      }

      const probe = await probeAudio(file);
      if (!probe.decodable) {
        setState({ phase: "error", message: "This file does not appear to be playable audio." });
        return;
      }
      if (probe.durationSeconds !== null && probe.durationSeconds > CLIENT_LIMITS.maxDurationSeconds) {
        setState({
          phase: "error",
          message: `That track is ${Math.round(probe.durationSeconds / 60)} minutes. The current limit is ${CLIENT_LIMITS.maxDurationSeconds / 60} minutes.`,
        });
        return;
      }

      setState({ phase: "selected", file, durationSeconds: probe.durationSeconds });
      startUpload(file);
    },
    [startUpload],
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDragOver(false);
      const file = event.dataTransfer.files?.[0];
      if (file) void handleFile(file);
    },
    [handleFile],
  );

  const remove = useCallback(() => {
    xhrRef.current?.abort();
    setState({ phase: "empty" });
  }, []);

  const statusMessage =
    state.phase === "uploading"
      ? `Uploading ${state.file.name} — ${state.percent}%`
      : state.phase === "uploaded"
        ? "Upload complete. Preparing your job…"
        : state.phase === "error"
          ? state.message
          : "";

  return (
    <div className="w-full max-w-2xl px-2">
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload audio file: drag and drop or press Enter to choose a file"
        onClick={() => state.phase === "empty" && inputRef.current?.click()}
        onKeyDown={(event) => {
          if (state.phase === "empty" && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={`relative flex items-center gap-3 rounded-2xl border border-dashed p-4 transition-all duration-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-purple-400 ${
          dragOver
            ? "border-purple-400 bg-[#1a1524]"
            : "border-[#2b2b34] bg-[#131317] hover:border-zinc-600"
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={CLIENT_LIMITS.acceptedExtensions.join(",")}
          className="sr-only"
          aria-label="Choose audio file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handleFile(file);
            event.target.value = "";
          }}
        />

        {state.phase === "empty" && (
          <div className="flex w-full flex-col items-center gap-1 py-6 text-center">
            <UploadIcon />
            <p className="text-sm text-zinc-300">
              Drop an audio file here or <span className="font-semibold text-purple-300">choose a file</span>
            </p>
            <p className="text-xs text-zinc-500">
              MP3, WAV, FLAC, OGG, M4A · up to {formatBytes(CLIENT_LIMITS.maxUploadBytes)} · max {CLIENT_LIMITS.maxDurationSeconds / 60} minutes
            </p>
          </div>
        )}

        {state.phase === "selected" && (
          <FileSummary file={state.file} durationSeconds={state.durationSeconds} onRemove={remove} />
        )}

        {state.phase === "uploading" && (
          <>
            <FileIcon />
            <div className="min-w-0 flex-1 text-left">
              <p className="truncate text-sm text-zinc-200">{state.file.name}</p>
              <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-valuenow={state.percent} aria-valuemin={0} aria-valuemax={100}>
                <div className="purple-gradient-btn h-full transition-all duration-200" style={{ width: `${state.percent}%` }} />
              </div>
            </div>
            <button type="button" onClick={remove} className="shrink-0 rounded-md px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200">Cancel</button>
          </>
        )}

        {state.phase === "uploaded" && (
          <>
            <FileIcon />
            <div className="min-w-0 flex-1 text-left">
              <p className="truncate text-sm text-zinc-200">{state.filename}</p>
              <p className="text-xs text-zinc-500">Uploaded locally. Starting separation…</p>
            </div>
            <RemoveButton onRemove={remove} />
          </>
        )}

        {state.phase === "error" && (
          <>
            <FileIcon error />
            <div className="min-w-0 flex-1 text-left"><p className="text-sm text-red-400">{state.message}</p><p className="text-xs text-zinc-500">Nothing was uploaded.</p></div>
            <RemoveButton onRemove={remove} label="Dismiss" />
          </>
        )}
      </div>
      <p aria-live="polite" className="mt-2 min-h-5 text-xs text-zinc-400">{statusMessage}</p>
    </div>
  );
}

function FileSummary({ file, durationSeconds, onRemove }: { file: File; durationSeconds: number | null; onRemove: () => void }) {
  return (
    <>
      <FileIcon />
      <div className="min-w-0 flex-1 text-left">
        <p className="truncate text-sm text-zinc-200">{file.name}</p>
        <p className="text-xs text-zinc-500">{formatBytes(file.size)}{durationSeconds !== null ? ` · ${Math.floor(durationSeconds / 60)}:${String(Math.round(durationSeconds % 60)).padStart(2, "0")}` : ""}</p>
      </div>
      <RemoveButton onRemove={onRemove} />
    </>
  );
}

function UploadIcon() {
  return <svg className="h-6 w-6 text-zinc-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" x2="12" y1="3" y2="15" /></svg>;
}

function FileIcon({ error = false }: { error?: boolean }) {
  return <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${error ? "bg-red-950/60 text-red-400" : "bg-purple-950/40 text-purple-300"}`}><svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">{error ? <><circle cx="12" cy="12" r="10" /><line x1="12" x2="12" y1="8" y2="12" /><line x1="12" x2="12" y1="16" y2="16" /></> : <path d="M9 18V5l12-2v13" />}</svg></div>;
}

function RemoveButton({ onRemove, label = "Remove" }: { onRemove: () => void; label?: string }) {
  return <button type="button" onClick={onRemove} className="shrink-0 rounded-md px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200">{label}</button>;
}
