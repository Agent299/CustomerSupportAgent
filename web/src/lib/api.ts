/**
 * Base URL of the backend API.
 * - Production (Caddy, same origin): built with NEXT_PUBLIC_API_BASE_URL="" → relative URLs.
 * - Local dev: unset → the API on localhost:8000.
 */
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type ApiStatus = "checking" | "ok" | "slow" | "down";

/** Pings /healthz; a request that times out is reported as "slow" and retried by callers. */
export async function checkHealth(timeoutMs = 5000): Promise<ApiStatus> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE_URL}/healthz`, {
      signal: controller.signal,
    });
    return res.ok ? "ok" : "down";
  } catch (err) {
    return err instanceof DOMException && err.name === "AbortError"
      ? "slow"
      : "down";
  } finally {
    clearTimeout(timer);
  }
}
