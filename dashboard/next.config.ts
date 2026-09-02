import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output keeps the runtime image small - it ships only the traced
  // server and its actual dependency closure, not all of node_modules.
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
