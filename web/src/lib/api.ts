export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type ApiStatus = "checking" | "ok" | "waking" | "down";

/**
 * Pings /healthz. On Render's free tier the API sleeps after 15 min idle and
 * takes ~1 min to wake, so a slow first response is reported as "waking".
 */
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
      ? "waking"
      : "down";
  } finally {
    clearTimeout(timer);
  }
}
