"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  askForPerson,
  ChatError,
  type ChatSession,
  type Citation,
  createSession,
  type DocsVersion,
  DOCS_VERSIONS,
  getTranscript,
  sendFeedback,
  sendMessage,
  streamTurn,
} from "@/lib/chat";

export type TurnState = "searching" | "streaming" | "answered" | "handoff" | "error";

export type Turn = {
  key: string;
  role: "user" | "bot";
  text: string;
  citations: Citation[];
  state: TurnState;
  messageId?: string;
  rating?: 1 | -1;
  firstTokenMs?: number; // measured in the browser, from send to first token
};

const SESSION_KEY = "support-chat:session";
const VERSION_KEY = "support-chat:version";

function load<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function save(key: string, value: unknown): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // private mode or storage blocked: the conversation just won't survive a reload
  }
}

let keySeq = 0;
const nextKey = () => `t${++keySeq}`;

export function useChat() {
  const [session, setSession] = useState<ChatSession | null>(null);
  const [version, setVersionState] = useState<DocsVersion>("9.0");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [handedOff, setHandedOff] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  // Restore the last conversation in this browser.
  useEffect(() => {
    const stored = load<DocsVersion>(VERSION_KEY);
    const saved = load<ChatSession>(SESSION_KEY);
    let cancelled = false;
    (async () => {
      if (stored && DOCS_VERSIONS.includes(stored)) setVersionState(stored);
      if (!saved) return;
      try {
        const transcript = await getTranscript(saved);
        if (cancelled) return;
        setSession(saved);
        setHandedOff(["ESCALATED", "AGENT_ACTIVE"].includes(transcript.status));
        setTurns(
          transcript.messages
            .filter((m) => m.role === "user" || m.role === "bot")
            .map((m) => ({
              key: nextKey(),
              role: m.role === "user" ? "user" : "bot",
              text: m.content,
              citations: m.citations,
              state: m.role === "bot" && m.route === "ESCALATE" ? "handoff" : "answered",
              messageId: m.id,
            })),
        );
      } catch {
        save(SESSION_KEY, null); // expired or from another server
      }
    })();
    return () => {
      cancelled = true;
      abort.current?.abort();
    };
  }, []);

  const setVersion = useCallback((v: DocsVersion) => {
    setVersionState(v);
    save(VERSION_KEY, v);
  }, []);

  const updateTurn = (key: string, change: (t: Turn) => Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.key === key ? { ...t, ...change(t) } : t)));

  /** Reads one turn's events into the bot turn ``botKey``. */
  const follow = useCallback(
    async (s: ChatSession, after: string, botKey: string, sentAt: number) => {
      abort.current = new AbortController();
      for await (const event of streamTurn(s, after, abort.current.signal)) {
        switch (event.type) {
          case "token":
            updateTurn(botKey, (t) => ({
              text: t.text + event.text,
              state: "streaming",
              firstTokenMs: t.firstTokenMs ?? Math.round(performance.now() - sentAt),
            }));
            break;
          case "citations":
            updateTurn(botKey, () => ({ citations: event.citations }));
            break;
          case "final":
            updateTurn(botKey, () => ({
              text: event.answer,
              citations: event.citations,
              state: "answered",
              messageId: event.message_id,
            }));
            break;
          case "escalated":
            // Replaces anything streamed for this turn (an answer that failed validation).
            updateTurn(botKey, () => ({
              text: event.message,
              citations: [],
              state: "handoff",
              messageId: event.message_id,
            }));
            setHandedOff(true);
            break;
          case "error":
            updateTurn(botKey, () => ({ text: event.message, state: "error" }));
            break;
        }
      }
    },
    [],
  );

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;
      setBusy(true);
      setNotice(null);
      const botKey = nextKey();
      setTurns((all) => [
        ...all,
        { key: nextKey(), role: "user", text: question, citations: [], state: "answered" },
        { key: botKey, role: "bot", text: "", citations: [], state: "searching" },
      ]);
      try {
        let s = session;
        if (!s) {
          s = await createSession(version);
          setSession(s);
          save(SESSION_KEY, s);
        }
        const sentAt = performance.now();
        const { stream_after } = await sendMessage(s, question, version);
        await follow(s, stream_after, botKey, sentAt);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        const message =
          err instanceof ChatError ? err.message : "The answer stopped unexpectedly. Send your question again.";
        updateTurn(botKey, () => ({ text: message, state: "error" }));
        if (err instanceof ChatError && err.status === 404) {
          save(SESSION_KEY, null);
          setSession(null);
        }
      } finally {
        setBusy(false);
      }
    },
    [busy, follow, session, version],
  );

  const handOff = useCallback(async () => {
    if (!session || busy || handedOff) return;
    setBusy(true);
    const botKey = nextKey();
    setTurns((all) => [...all, { key: botKey, role: "bot", text: "", citations: [], state: "searching" }]);
    try {
      const after = await askForPerson(session);
      await follow(session, after, botKey, performance.now());
    } catch (err) {
      updateTurn(botKey, () => ({
        text: err instanceof ChatError ? err.message : "Couldn't reach a person. Try again.",
        state: "error",
      }));
    } finally {
      setBusy(false);
    }
  }, [busy, follow, handedOff, session]);

  const rate = useCallback(
    async (turn: Turn, rating: 1 | -1) => {
      if (!session || !turn.messageId) return;
      updateTurn(turn.key, () => ({ rating }));
      try {
        await sendFeedback(session, turn.messageId, rating);
      } catch {
        updateTurn(turn.key, () => ({ rating: undefined }));
        setNotice("Your rating wasn't saved. Try again.");
      }
    },
    [session],
  );

  const restart = useCallback(() => {
    abort.current?.abort();
    save(SESSION_KEY, null);
    setSession(null);
    setTurns([]);
    setHandedOff(false);
    setNotice(null);
    setBusy(false);
  }, []);

  return { session, version, setVersion, turns, busy, handedOff, notice, send, handOff, rate, restart };
}
