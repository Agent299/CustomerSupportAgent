"use client";

import { type FormEvent, type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { type Citation, type DocsVersion, DOCS_VERSIONS } from "@/lib/chat";
import { type CitationRef, Markdown } from "@/lib/markdown";
import { type Turn, useChat } from "@/lib/useChat";

const EXAMPLES = [
  "What does E11000 duplicate key error mean?",
  "How do I make documents expire an hour after they're created?",
  "My $group stage fails with a memory limit error. What can I do?",
];

export function ChatWidget() {
  const chat = useChat();
  const end = useRef<HTMLDivElement>(null);
  const last = chat.turns.at(-1);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [chat.turns.length, last?.text.length]);

  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-6xl flex-col px-4 sm:px-8">
      <header className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4 border-b border-rule pb-5 pt-8">
        <div className="max-w-prose">
          <h1 className="text-2xl font-semibold tracking-tight">Ask the MongoDB docs</h1>
          <p className="mt-1 text-muted">
            Answers come from the MongoDB server manuals, with a numbered source for every claim.
            Questions the docs can&apos;t answer go to a person.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <VersionSwitch value={chat.version} onChange={chat.setVersion} />
          {chat.turns.length > 0 && (
            <button type="button" onClick={chat.restart} className="text-sm text-muted underline underline-offset-2 hover:text-ink">
              New conversation
            </button>
          )}
        </div>
      </header>

      <main className="flex-1 py-8" aria-live="polite" aria-busy={chat.busy}>
        {chat.turns.length === 0 ? (
          <EmptyState onPick={chat.send} />
        ) : (
          <ol className="flex flex-col gap-10">
            {chat.turns.map((turn) => (
              <li key={turn.key}>
                {turn.role === "user" ? <UserTurn turn={turn} /> : <BotTurn turn={turn} onRate={chat.rate} />}
              </li>
            ))}
          </ol>
        )}
        {chat.notice && <p className="mt-6 text-sm text-danger">{chat.notice}</p>}
        <div ref={end} />
      </main>

      <Composer
        busy={chat.busy}
        handedOff={chat.handedOff}
        canHandOff={chat.session !== null}
        onSend={chat.send}
        onHandOff={chat.handOff}
      />
      <p className="pb-4 text-xs text-muted">
        Built on MongoDB documentation (CC BY-NC-SA). Not affiliated with MongoDB, Inc. Don&apos;t share
        passwords or keys; emails and phone numbers are masked before anything is stored.
      </p>
    </div>
  );
}

function VersionSwitch({ value, onChange }: { value: DocsVersion; onChange: (v: DocsVersion) => void }) {
  return (
    <fieldset className="flex items-center gap-2">
      <legend className="sr-only">MongoDB version</legend>
      <span className="text-sm text-muted" aria-hidden>
        MongoDB
      </span>
      <div className="flex rounded-md border border-rule bg-page p-0.5">
        {DOCS_VERSIONS.map((v) => (
          <label
            key={v}
            className="cursor-pointer rounded px-2.5 py-1 text-sm has-checked:bg-ink has-checked:text-page has-focus-visible:outline-2 has-focus-visible:outline-cite"
          >
            <input
              type="radio"
              name="docs-version"
              value={v}
              checked={value === v}
              onChange={() => onChange(v)}
              className="sr-only"
            />
            {v}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="max-w-prose">
      <p className="text-lg">Describe the problem, paste the error, or ask how to do something.</p>
      <p className="mt-6 text-sm text-muted">Or start with one of these:</p>
      <ul className="mt-2 flex flex-col gap-2">
        {EXAMPLES.map((q) => (
          <li key={q}>
            <button
              type="button"
              onClick={() => onPick(q)}
              className="text-left text-cite underline decoration-cite/40 underline-offset-4 hover:decoration-cite"
            >
              {q}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function UserTurn({ turn }: { turn: Turn }) {
  return (
    <div className="max-w-[70ch] border-l-2 border-ink/70 bg-page/60 py-2 pl-4 pr-3">
      <span className="sr-only">You: </span>
      <p className="whitespace-pre-wrap">{turn.text}</p>
    </div>
  );
}

/** Pages in first-cited order; several chunks of one page share its number. */
function useSources(citations: Citation[]) {
  return useMemo(() => {
    const pages: { url: string; title: string; versions: string[] }[] = [];
    const byChunk = new Map<string, CitationRef>();
    for (const c of citations) {
      let n = pages.findIndex((p) => p.url === c.url) + 1;
      if (n === 0) {
        pages.push({ url: c.url, title: c.title, versions: c.versions });
        n = pages.length;
      }
      byChunk.set(c.chunk_id, { n, url: c.url, title: c.title });
    }
    return { pages, ref: (id: string) => byChunk.get(id) };
  }, [citations]);
}

function BotTurn({ turn, onRate }: { turn: Turn; onRate: (t: Turn, r: 1 | -1) => void }) {
  const { pages, ref } = useSources(turn.citations);

  if (turn.state === "searching") {
    return <p className="text-muted">Searching the docs…</p>;
  }
  if (turn.state === "handoff") {
    return (
      <div role="status" className="max-w-[70ch] border-l-4 border-handoff bg-handoff-soft px-4 py-3">
        <p className="font-medium text-handoff">Handed to a support engineer</p>
        <p className="mt-1">{turn.text}</p>
      </div>
    );
  }
  if (turn.state === "error") {
    return (
      <p role="alert" className="max-w-[70ch] text-danger">
        {turn.text}
      </p>
    );
  }

  return (
    <article className="grid gap-x-10 gap-y-4 lg:grid-cols-[minmax(0,70ch)_16rem]">
      <div>
        <div className={`answer ${turn.state === "streaming" ? "streaming" : ""}`}>
          <Markdown text={turn.text} refs={ref} />
        </div>
        {turn.state === "answered" && turn.messageId && (
          <Rating turn={turn} onRate={onRate} />
        )}
      </div>
      {pages.length > 0 && (
        <aside aria-label="Sources" className="border-t border-rule pt-3 text-sm lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
          <ol className="flex flex-col gap-3">
            {pages.map((page, i) => (
              <li key={page.url} className="flex gap-2">
                <span className="cite-mark mt-1 h-fit shrink-0 !align-baseline" aria-hidden>
                  {i + 1}
                </span>
                <span>
                  <a href={page.url} target="_blank" rel="noopener noreferrer" className="font-medium hover:underline">
                    {page.title || page.url}
                  </a>
                  <span className="block text-xs text-muted">
                    MongoDB {page.versions.join(", ")}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </aside>
      )}
    </article>
  );
}

function Rating({ turn, onRate }: { turn: Turn; onRate: (t: Turn, r: 1 | -1) => void }) {
  if (turn.rating) {
    return (
      <p className="mt-4 text-sm text-muted">
        {turn.rating === 1 ? "Thanks, marked as helpful." : "Thanks, marked as not helpful. It goes to review."}
      </p>
    );
  }
  return (
    <div className="mt-4 flex items-center gap-3 text-sm text-muted">
      <span>Did this answer your question?</span>
      <button type="button" onClick={() => onRate(turn, 1)} className="rounded border border-rule px-2 py-0.5 hover:border-ink hover:text-ink">
        Yes
      </button>
      <button type="button" onClick={() => onRate(turn, -1)} className="rounded border border-rule px-2 py-0.5 hover:border-ink hover:text-ink">
        No
      </button>
    </div>
  );
}

function Composer({
  busy,
  handedOff,
  canHandOff,
  onSend,
  onHandOff,
}: {
  busy: boolean;
  handedOff: boolean;
  canHandOff: boolean;
  onSend: (text: string) => Promise<void>;
  onHandOff: () => Promise<void>;
}) {
  const [text, setText] = useState("");

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || busy) return;
    void onSend(text);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit();
  };

  return (
    <form onSubmit={submit} className="sticky bottom-0 border-t border-rule bg-paper pb-3 pt-4">
      {handedOff && (
        <p className="mb-2 text-sm text-handoff">
          A support engineer has this conversation. Messages you send now wait for them.
        </p>
      )}
      <label htmlFor="question" className="sr-only">
        Your question
      </label>
      <div className="flex items-end gap-3">
        <textarea
          id="question"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          maxLength={4000}
          placeholder="Ask about MongoDB, or paste an error message"
          className="min-h-[3.25rem] flex-1 resize-y rounded-md border border-rule bg-page px-3 py-2 placeholder:text-muted focus-visible:border-cite"
        />
        <button
          type="submit"
          disabled={busy || !text.trim()}
          className="h-[3.25rem] rounded-md bg-ink px-5 font-medium text-page disabled:opacity-40"
        >
          {busy ? "Answering…" : "Send"}
        </button>
      </div>
      <div className="mt-2 flex justify-between text-xs text-muted">
        <span>Enter sends, Shift+Enter adds a line</span>
        {canHandOff && !handedOff && (
          <button type="button" onClick={() => void onHandOff()} disabled={busy} className="underline underline-offset-2 hover:text-ink disabled:opacity-40">
            Talk to a person
          </button>
        )}
      </div>
    </form>
  );
}
