import { Suspense } from "react";
import { MemoryWorkbench } from "@/components/memory/memory-workbench";

export default function MemoryPage() {
  return <Suspense fallback={<div className="memory-empty">正在打开记忆工作台…</div>}><MemoryWorkbench /></Suspense>;
}
