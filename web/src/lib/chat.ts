/**
 * Client for the chat API (docs/PLAN.md §8.2). The stream is read with fetch(), not
 * EventSource, because it must send the X-Session-Token header.
 */
import { API_BASE_URL } from "@/lib/api";

const CHAT = `${API_BASE_URL}/api/v1/chat`;

export type DocsVersion = "9.0" | "8.0" | "7.0";
export const DOCS_VERSIONS: DocsVersion[] = ["9.0", "8.0", "7.0"];

export type Citation = {
  chunk_id: string;
  url: string;
  title: string;
  versions: string[];
};

export type ChatSession = { id: string; token: string };

export type TurnEvent =
  | { type: "ack"; message_id: string }
  | { type: "token"; text: string }
  | { type: "citations"; citations: Citation[] }
  | {
      type: "final";
      message_id: string;
      answer: string;
      citations: Citation[];
      resolved_version: string;
      first_token_ms: number | null;
      latency_ms: number;
    }
  | { type: "escalated"; message: string; reason?: string; message_id?: string }
  | { type: "error"; code: string; message: string };

export type TranscriptMessage = {
  id: string;
  role: "user" | "bot" | "agent" | "system";
  content: string;
  citations: Citation[];
  route: "ANSWERED" | "ESCALATE" | null;
};

export type Transcript = {
  session_id: string;
  status: string;
  messages: TranscriptMessage[];
};

/** A failed request, with a message written for the person using the widget. */
export class ChatError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const MESSAGES: Record<number, string> = {
  404: "This conversation no longer exists. Start a new one.",
  409: "Wait for the current answer to finish, then send your next message.",
  503: "Chat is switched off on this server: no answer model is configured.",
};

async function request(path: string, init: RequestInit, session?: ChatSession) {
  const headers = new Headers(init.headers);
  if (session) headers.set("X-Session-Token", session.token);
  if (init.body) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(`${CHAT}${path}`, { ...init, headers });
  } catch {
    throw new ChatError(`Can't reach the support API at ${API_BASE_URL}.`, 0);
  }
  if (!res.ok) {
    throw new ChatError(
      MESSAGES[res.status] ?? `The support API answered ${res.status}.`,
      res.status,
    );
  }
  return res;
}

export async function createSession(version: DocsVersion): Promise<ChatSession> {
  const res = await request("/sessions", {
    method: "POST",
    body: JSON.stringify({ version_hint: version }),
  });
  const data = await res.json();
  return { id: data.session_id, token: data.session_token };
}

export async function getTranscript(session: ChatSession): Promise<Transcript> {
  const res = await request(`/sessions/${session.id}`, {}, session);
  return res.json();
}

export async function sendMessage(
  session: ChatSession,
  text: string,
  version: DocsVersion,
): Promise<{ message_id: string; stream_after: string }> {
  const res = await request(
    `/sessions/${session.id}/messages`,
    { method: "POST", body: JSON.stringify({ text, version_hint: version }) },
    session,
  );
  return res.json();
}

/** Hands the conversation to a person; read the stream from the returned cursor. */
export async function askForPerson(session: ChatSession): Promise<string> {
  const res = await request(
    `/sessions/${session.id}/escalate`,
    { method: "POST" },
    session,
  );
  return (await res.json()).stream_after;
}

export async function sendFeedback(
  session: ChatSession,
  messageId: string,
  rating: 1 | -1,
): Promise<void> {
  await request(
    `/messages/${messageId}/feedback`,
    { method: "POST", body: JSON.stringify({ rating }) },
    session,
  );
}

/** Events of one turn, read from `after`; ends after final, escalated or error. */
export async function* streamTurn(
  session: ChatSession,
  after: string,
  signal?: AbortSignal,
): AsyncGenerator<TurnEvent> {
  const res = await request(
    `/sessions/${session.id}/stream?after=${encodeURIComponent(after)}`,
    { signal },
    session,
  );
  if (!res.body) throw new ChatError("The browser can't read streamed answers.", 0);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const event = parseEvent(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (event) yield event;
    }
  }
}

function parseEvent(block: string): TurnEvent | null {
  let type = "";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) type = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!type || !data) return null; // keep-alive comment
  return { type, ...JSON.parse(data) } as TurnEvent;
}

