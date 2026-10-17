import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the UI is plain files fetching data client-side, so any static host works later.
  output: "export",
  trailingSlash: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
