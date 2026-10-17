import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Deployed as a Render Static Site (docs/PLAN.md §9); all data is fetched client-side.
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
