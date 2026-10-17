import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export served by Caddy on the Oracle VM (deploy/oracle); data is fetched client-side.
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
