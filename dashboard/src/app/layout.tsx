import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TraceMap",
  description:
    "A dependency map that shows which edge is slow AND on the critical path.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
