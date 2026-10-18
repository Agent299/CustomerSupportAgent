/**
 * The Markdown subset answers use, rendered to React elements: no HTML string ever reaches
 * the DOM, so model or docs text cannot inject markup (docs/PLAN.md §8.9).
 *
 * Blocks: fenced code, headings, bullet and numbered lists, paragraphs.
 * Inline: `code`, **bold**, [text](https://…), and citation markers [c:<id>], which become
 * numbered references in first-cited order.
 */
import { Fragment, type ReactNode } from "react";

export type CitationRef = { n: number; url?: string; title?: string };

const INLINE =
  /`([^`]+)`|\*\*([^*]+)\*\*|\[c:([0-9a-f]{16})\]|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
const PARTIAL_MARKER = /\[(?:c(?::[0-9a-f]{0,16})?)?$/; // a marker still streaming in

/** Citation ids in first-appearance order: the numbering used in the text and the margin. */
export function citationOrder(text: string): string[] {
  const ids: string[] = [];
  for (const m of text.matchAll(/\[c:([0-9a-f]{16})\]/g)) {
    if (!ids.includes(m[1])) ids.push(m[1]);
  }
  return ids;
}

export function Markdown({
  text,
  refs,
}: {
  text: string;
  refs: (chunkId: string) => CitationRef | undefined;
}) {
  return <>{blocks(text.replace(PARTIAL_MARKER, ""), refs)}</>;
}

function blocks(text: string, refs: (id: string) => CitationRef | undefined): ReactNode[] {
  const out: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) code.push(lines[i++]);
      i++; // closing fence (absent while streaming)
      out.push(
        <pre key={out.length} className="md-code" data-lang={fence[1] || undefined}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      out.push(
        <p key={out.length} className="md-heading">
          {inline(heading[2], refs)}
        </p>,
      );
      i++;
      continue;
    }
    const listItem = /^\s*(?:[-*]|\d+[.)])\s+/;
    if (listItem.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && listItem.test(lines[i])) {
        items.push(lines[i].replace(listItem, ""));
        i++;
        // continuation lines indented under the item
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !listItem.test(lines[i])) {
          items[items.length - 1] += " " + lines[i].trim();
          i++;
        }
      }
      const List = ordered ? "ol" : "ul";
      out.push(
        <List key={out.length} className={ordered ? "md-ol" : "md-ul"}>
          {items.map((item, k) => (
            <li key={k}>{inline(item, refs)}</li>
          ))}
        </List>,
      );
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```/.test(lines[i]) &&
      !/^#{1,4}\s/.test(lines[i]) &&
      !listItem.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    out.push(<p key={out.length}>{inline(para.join(" "), refs)}</p>);
  }
  return out;
}

function inline(text: string, refs: (id: string) => CitationRef | undefined): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const key = `${start}`;
    if (m[1] !== undefined) {
      out.push(<code key={key}>{m[1]}</code>);
    } else if (m[2] !== undefined) {
      out.push(<strong key={key}>{m[2]}</strong>);
    } else if (m[3] !== undefined) {
      const ref = refs(m[3]);
      if (ref) out.push(<CitationMark key={key} citation={ref} />);
    } else {
      out.push(
        <a key={key} href={m[5]} target="_blank" rel="noopener noreferrer">
          {m[4]}
        </a>,
      );
    }
    last = start + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.map((node, k) => <Fragment key={k}>{node}</Fragment>);
}

function CitationMark({ citation }: { citation: CitationRef }) {
  const label = `Source ${citation.n}${citation.title ? `: ${citation.title}` : ""}`;
  return citation.url ? (
    <a
      className="cite-mark"
      href={citation.url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={label}
      title={label}
    >
      {citation.n}
    </a>
  ) : (
    <span className="cite-mark" aria-label={label}>
      {citation.n}
    </span>
  );
}
