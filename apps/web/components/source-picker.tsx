"use client";

/**
 * Source picker (plan §5.1/§8.1): YouTube import stays visible but secondary;
 * upload is the primary flow. Recreates the design's tab switcher.
 */
import { useState } from "react";

import { UploadDropzone } from "@/components/upload-dropzone";

export function SourcePicker() {
  const [mode, setMode] = useState<"youtube" | "upload">("youtube");
  const [youtubeUrl, setYoutubeUrl] = useState("");

  return (
    <div className="flex w-full flex-col items-center">
      {/* Input Mode Switcher (Tabs) */}
      <div className="mb-6 flex items-center rounded-full border border-zinc-800/80 bg-[#151518] p-1">
        <button
          type="button"
          onClick={() => setMode("youtube")}
          aria-pressed={mode === "youtube"}
          className={`flex items-center gap-2 rounded-full px-5 py-2 text-xs transition ${
            mode === "youtube"
              ? "bg-[#202025] font-semibold text-white shadow-sm"
              : "font-medium text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <svg
            className="h-3.5 w-3.5 text-zinc-300"
            fill="none"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
          </svg>
          <span>YouTube Link</span>
        </button>
        <button
          type="button"
          onClick={() => setMode("upload")}
          aria-pressed={mode === "upload"}
          className={`flex items-center gap-2 rounded-full px-5 py-2 text-xs transition ${
            mode === "upload"
              ? "bg-[#202025] font-semibold text-white shadow-sm"
              : "font-medium text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <svg
            className="h-3.5 w-3.5 text-zinc-500"
            fill="none"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="17 8 12 3 7 8" />
            <line x1="12" x2="12" y1="3" y2="15" />
          </svg>
          <span>Upload File</span>
        </button>
      </div>

      {mode === "youtube" ? (
        /* Main Input Bar — original design, YouTube mode */
        <div className="w-full max-w-2xl px-2">
          <div className="relative flex items-center rounded-full border border-[#2b2b34] bg-[#131317] p-1.5 shadow-2xl transition-all duration-200 focus-within:border-zinc-500">
            <div className="pl-4 pr-2 text-zinc-500">
              <svg
                className="h-4 w-4"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                viewBox="0 0 24 24"
              >
                <rect height="18" rx="2" width="18" x="3" y="3" />
                <path d="M9 3v18" />
              </svg>
            </div>
            <input
              className="w-full border-none bg-transparent px-1 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:ring-0"
              placeholder="Paste a YouTube link or drop an audio file"
              type="text"
              value={youtubeUrl}
              onChange={(e) => setYoutubeUrl(e.target.value)}
            />
            <button
              className="purple-gradient-btn shrink-0 rounded-full px-6 py-2.5 text-xs font-semibold text-white transition-all duration-200 active:scale-[0.98] sm:text-sm"
              type="button"
            >
              Extract Stems
            </button>
          </div>
        </div>
      ) : (
        <UploadDropzone />
      )}
    </div>
  );
}
