import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Self-hosted variable fonts (OFL-licensed, latin subset): the app must boot
// with no network access, so Google Fonts is not fetched at build/dev time.
const inter = localFont({
  src: "./fonts/inter-latin-wght-normal.woff2",
  variable: "--font-inter",
  weight: "100 900",
  display: "swap",
});

const jetbrainsMono = localFont({
  src: "./fonts/jetbrains-mono-latin-wght-normal.woff2",
  variable: "--font-jetbrains-mono",
  weight: "100 800",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Stemify - Extract Stems with Precision",
  description:
    "Professional-grade audio separation. Paste a YouTube link or upload your own files to get started.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-screen flex flex-col justify-between selection:bg-purple-600 selection:text-white font-sans antialiased">
        {children}
      </body>
    </html>
  );
}
