import type { Metadata } from "next";

import { ChatWidget } from "@/components/chat/ChatWidget";

export const metadata: Metadata = { title: "Ask the MongoDB docs" };

export default function WidgetPage() {
  return <ChatWidget />;
}
