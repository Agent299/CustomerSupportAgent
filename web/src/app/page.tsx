import Link from "next/link";

import { ApiStatusBadge } from "@/components/ApiStatusBadge";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-4 py-16">
      <h1 className="text-3xl font-semibold">MongoDB Support Agent</h1>
      <p>
        A GraphRAG customer support agent: hybrid search plus a knowledge graph
        over the MongoDB docs, with cited answers and handoff to a human.
      </p>
      <ApiStatusBadge />
      <nav className="flex gap-4 underline">
        <Link href="/widget/">Chat widget</Link>
        <Link href="/console/">Agent console</Link>
      </nav>
      <footer className="mt-auto text-sm opacity-70">
        Answers derived from MongoDB documentation (CC BY-NC-SA). Not
        affiliated with MongoDB, Inc. Non-commercial portfolio project.
      </footer>
    </main>
  );
}
