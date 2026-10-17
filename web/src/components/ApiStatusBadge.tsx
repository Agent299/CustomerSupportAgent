"use client";

import { useEffect, useState } from "react";

import { type ApiStatus, checkHealth } from "@/lib/api";

const LABELS: Record<ApiStatus, string> = {
  checking: "Checking API…",
  ok: "API online",
  slow: "API is slow to respond, retrying…",
  down: "API unreachable",
};

export function ApiStatusBadge() {
  const [status, setStatus] = useState<ApiStatus>("checking");

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const next = await checkHealth();
      if (cancelled) return;
      setStatus(next);
      if (next === "slow") setTimeout(poll, 5000);
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <span role="status" className="rounded-full border px-3 py-1 text-sm">
      {LABELS[status]}
    </span>
  );
}
