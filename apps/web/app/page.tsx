import { SourcePicker } from "@/components/source-picker";

export default function Home() {
  return (
    <>
      {/* BEGIN: MainHeader */}
      <header className="w-full px-6 py-5 md:px-10 flex items-center justify-between z-20">
        {/* Brand Logo and Name */}
        <div className="flex items-center gap-2.5 cursor-pointer">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-tr from-violet-600 to-fuchsia-500 flex items-center justify-center shadow-lg shadow-purple-900/30">
            {/* Stylized Soundwave / Stem icon */}
            <svg
              className="w-4 h-4 text-white"
              fill="none"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2.5}
              viewBox="0 0 24 24"
            >
              <path d="M4 10v4" />
              <path d="M8 7v10" />
              <path d="M12 4v16" />
              <path d="M16 8v8" />
              <path d="M20 11v2" />
            </svg>
          </div>
          <span className="text-white font-extrabold tracking-wider text-base uppercase">
            Stemify
          </span>
        </div>
        {/* Engine Version Badge */}
        <div className="flex items-center">
          <span className="px-3 py-1 text-[11px] font-mono font-semibold tracking-wider text-zinc-400 bg-zinc-900/80 border border-zinc-800 rounded-md hover:border-zinc-700 transition-colors cursor-default">
            V2.4.0 ENGINE
          </span>
        </div>
      </header>
      {/* END: MainHeader */}

      {/* BEGIN: HeroSection */}
      <main className="w-full max-w-4xl mx-auto px-4 py-8 flex flex-col items-center justify-center text-center flex-grow -mt-6">
        {/* Title and Subtitle Description */}
        <div className="space-y-3 mb-8">
          <h1 className="text-4xl sm:text-5xl md:text-[54px] font-extrabold tracking-tight text-white leading-tight">
            Extract stems with precision.
          </h1>
          <p className="text-zinc-400 text-sm sm:text-base max-w-lg mx-auto font-normal leading-relaxed">
            Professional-grade audio separation. Paste a YouTube link or upload
            your own files to get started.
          </p>
        </div>

        {/* Input Mode Switcher + Upload/YouTube flows */}
        <SourcePicker />

        {/* Feature Badges */}
        <div className="mt-10 flex flex-wrap items-center justify-center gap-6 md:gap-8 text-zinc-500 text-[11px] font-semibold tracking-wider">
          {/* Feature 1: Fast Processing */}
          <div className="flex items-center gap-2">
            <svg
              className="w-3.5 h-3.5 text-zinc-500"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
            <span>FAST PROCESSING</span>
          </div>
          {/* Feature 2: Private & Secure */}
          <div className="flex items-center gap-2">
            <svg
              className="w-3.5 h-3.5 text-zinc-500"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <rect height="11" rx="2" ry="2" width="18" x="3" y="11" />
              <path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
            <span>PRIVATE &amp; SECURE</span>
          </div>
          {/* Feature 3: High Fidelity */}
          <div className="flex items-center gap-2">
            <svg
              className="w-3.5 h-3.5 text-zinc-500"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <circle cx="12" cy="12" r="10" />
              <polygon points="10 8 16 12 10 16 10 8" />
            </svg>
            <span>HIGH FIDELITY</span>
          </div>
        </div>
      </main>
      {/* END: HeroSection */}

      {/* BEGIN: MainFooter */}
      <footer className="w-full border-t border-zinc-900/80 px-6 py-5 md:px-10 flex flex-col sm:flex-row items-center justify-between text-xs text-zinc-500 gap-3">
        {/* Copyright */}
        <div>© 2025 Stemify</div>
        {/* Footer Links */}
        <nav className="flex items-center space-x-6">
          <a className="hover:text-zinc-300 transition-colors" href="#">
            Term
          </a>
          <a className="hover:text-zinc-300 transition-colors" href="#">
            Privacy
          </a>
          <a className="hover:text-zinc-300 transition-colors" href="#">
            Docs
          </a>
        </nav>
      </footer>
      {/* END: MainFooter */}
    </>
  );
}
