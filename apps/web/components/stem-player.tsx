"use client";

/**
 * Per-stem player for the completed view. The native `<audio controls>` bar
 * renders as a light system widget that clashes with the dark app, so the
 * element stays in the DOM as the engine (time, seeking, volume) but without
 * its chrome, and the lane draws its own: a colored waveform — real peaks,
 * decoded from the stem file in the browser — that doubles as the seek bar,
 * plus a transport row in the app's own type.
 *
 * When decoding is unavailable (no Web Audio, an exotic codec, or a DOM
 * without canvas — tests fall here), the lane shows uniform placeholder bars
 * rather than invented data, and transport still works through the engine.
 */
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

export type StemPlayerStem = {
  id: string;
  label: string;
  durationSeconds: number | null;
};

/** Lane colors per stem id, matched across the app (dot, waveform, playhead). */
const STEM_COLORS: Record<string, string> = {
  vocals: "#2dd4bf",
  drums: "#fb7185",
  bass: "#fbbf24",
  instrumental: "#a78bfa",
  // drum_breakdown refinements
  kick: "#f43f5e",
  snare: "#38bdf8",
  cymbals: "#fbbf24",
  toms: "#fb923c",
};

const DEFAULT_COLOR = "#a78bfa";

export function stemColor(stemId: string): string {
  return STEM_COLORS[stemId] ?? DEFAULT_COLOR;
}

const WAVE_BUCKETS = 160;

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Peak amplitudes (0..1) per bucket from the left channel, normalized. */
async function computePeaks(src: string, buckets: number): Promise<number[] | null> {
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    const response = await fetch(src);
    if (!response.ok) return null;
    const buffer = await response.arrayBuffer();
    const context = new Ctor();
    try {
      const audio = await context.decodeAudioData(buffer);
      const channel = audio.getChannelData(0);
      const step = Math.floor(channel.length / buckets) || 1;
      const peaks: number[] = [];
      for (let i = 0; i < buckets; i++) {
        let max = 0;
        // Sample within the bucket: every 16th frame is plenty for 160 bars.
        for (let j = i * step; j < (i + 1) * step && j < channel.length; j += 16) {
          const v = Math.abs(channel[j]);
          if (v > max) max = v;
        }
        peaks.push(max);
      }
      const loudest = Math.max(...peaks, 0.0001);
      return peaks.map((peak) => Math.min(1, peak / loudest));
    } finally {
      void context.close();
    }
  } catch {
    return null;
  }
}

export function StemPlayer({
  jobId,
  stem,
  className = "",
}: {
  jobId: string;
  stem: StemPlayerStem;
  className?: string;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const waveRef = useRef<HTMLCanvasElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState<number | null>(stem.durationSeconds);
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [noCanvas, setNoCanvas] = useState(false);
  const color = stemColor(stem.id);
  const src = `/api/jobs/${jobId}/downloads?kind=stem&stem=${stem.id}`;

  // Real waveform: decode once on mount. The <audio> element re-streams on
  // play, but this fetch is served from cache in practice; it is a local file.
  useEffect(() => {
    let cancelled = false;
    void computePeaks(src, WAVE_BUCKETS).then((result) => {
      if (!cancelled) setPeaks(result);
    });
    return () => {
      cancelled = true;
    };
  }, [src]);

  // Engine state → render. Events (not the play() promise) stay the source of
  // truth for pause/ended so the button can never stick in a playing state.
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => setCurrent(audio.currentTime);
    const onMeta = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) setDuration(audio.duration);
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => {
      setPlaying(false);
      setCurrent(0);
    };
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("loadedmetadata", onMeta);
    audio.addEventListener("durationchange", onMeta);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("loadedmetadata", onMeta);
      audio.removeEventListener("durationchange", onMeta);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
    };
  }, []);

  const played = duration && duration > 0 ? Math.min(1, current / duration) : 0;

  // Draw the lane: dim bars ahead of the playhead, lit bars behind it.
  useEffect(() => {
    const canvas = waveRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      setNoCanvas(true);
      return;
    }
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 56;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const bars = peaks ?? [];
    const bucketWidth = bars.length > 0 ? width / bars.length : width / WAVE_BUCKETS;
    const barWidth = Math.max(1.5, bucketWidth * 0.62);
    const playX = played * width;
    for (let i = 0; i < (bars.length || WAVE_BUCKETS); i++) {
      const amplitude = bars.length > 0 ? bars[i] : 0.14;
      const barHeight = Math.max(2, amplitude * (height - 8));
      const x = i * bucketWidth + (bucketWidth - barWidth) / 2;
      const y = (height - barHeight) / 2;
      ctx.fillStyle = x + barWidth <= playX ? color : `${color}40`;
      if (typeof ctx.roundRect === "function") {
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, barWidth / 2);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, barWidth, barHeight);
      }
    }
    if (played > 0) {
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      ctx.fillRect(Math.min(playX, width - 1.5), 2, 1.5, height - 4);
    }
  }, [peaks, played, color]);

  function togglePlay() {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      setPlaying(true); // optimistic; the pause event corrects a rejected play
      // jsdom (tests) and some WebViews return undefined, not a promise.
      const request = audio.play() as unknown as Promise<void> | undefined;
      if (request && typeof request.catch === "function") {
        request.catch(() => setPlaying(false));
      }
    } else {
      audio.pause();
    }
  }

  function seek(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.buttons !== 1 && event.type !== "pointerdown") return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const audio = audioRef.current;
    const total =
      audio && Number.isFinite(audio.duration) && audio.duration > 0
        ? audio.duration
        : (duration ?? 0);
    if (!total) return;
    if (audio) audio.currentTime = fraction * total;
    setCurrent(fraction * total);
  }

  return (
    <section
      className={`rounded-2xl border border-zinc-800/80 bg-[#0d0d10] px-3 py-3 ${className}`}
      aria-label={`Preview ${stem.label}`}
    >
      <audio ref={audioRef} preload="none" src={src} className="hidden" />

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={togglePlay}
          aria-label={playing ? `Pause ${stem.label}` : `Play ${stem.label}`}
          className="flex size-9 shrink-0 items-center justify-center rounded-full text-black transition enabled:hover:brightness-110"
          style={{ background: color }}
        >
          {playing ? (
            <svg viewBox="0 0 24 24" className="size-4" fill="currentColor" aria-hidden="true">
              <rect x="6" y="5" width="4" height="14" rx="1" />
              <rect x="14" y="5" width="4" height="14" rx="1" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" className="ml-0.5 size-4" fill="currentColor" aria-hidden="true">
              <path d="M8 5.5v13a1 1 0 0 0 1.54.84l10-6.5a1 1 0 0 0 0-1.68l-10-6.5A1 1 0 0 0 8 5.5Z" />
            </svg>
          )}
        </button>

        <span className="w-[86px] shrink-0 font-mono text-[11px] tabular-nums text-zinc-400">
          {formatTime(current)} / {formatTime(duration ?? 0)}
        </span>

        {/* The waveform is the seek bar. */}
        <div
          className="relative h-14 min-w-0 flex-1 overflow-hidden rounded-lg bg-black/40"
          onPointerDown={seek}
          onPointerMove={seek}
          role="slider"
          aria-label={`Seek ${stem.label}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(played * 100)}
          tabIndex={0}
        >
          <canvas
            ref={waveRef}
            className={`h-14 w-full ${noCanvas ? "hidden" : ""}`}
            aria-hidden="true"
          />
          {noCanvas ? (
            /* Uniform bars: a placeholder for "no waveform available", not
               invented audio data. */
            <div
              data-waveform="placeholder"
              className="h-14 w-full"
              style={{
                background: `repeating-linear-gradient(90deg, ${color}40 0 3px, transparent 3px 7px)`,
              }}
            />
          ) : null}
        </div>
      </div>
    </section>
  );
}
