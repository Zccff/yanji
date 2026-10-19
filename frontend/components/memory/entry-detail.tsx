"use client";

import { useEffect, useState } from "react";
import { Clock3, FileClock, History, Pencil, Pin, RotateCcw, Trash2 } from "lucide-react";
import { dateLabel, describeValue, futureScore, kindLabels, memoryPath, memoryRequest, statusLabels, type MemoryEntry, type MemoryRevision } from "@/lib/memory";

export function EntryDetail({ entry, repoId, busy, onEdit, onForget, onRestore, onPin, onRevert }: {
  entry: MemoryEntry; repoId: string; busy: boolean; onEdit: () => void; onForget: () => void; onRestore: () => void; onPin: () => void; onRevert: (version: number) => void;
}) {
  const [revisions, setRevisions] = useState<MemoryRevision[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  useEffect(() => {
    if (!historyOpen) return;
    let cancelled = false;
    setLoading(true); setError(""); setRevisions([]);
    memoryRequest<MemoryRevision[]>(memoryPath(repoId, `entries/${entry.id}/revisions`)).then((data) => { if (!cancelled) setRevisions(data); }).catch((reason: Error) => { if (!cancelled) setError(reason.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [repoId, entry.id, entry.version, historyOpen]);
  const score = Math.max(0, Math.min(1, entry.score ?? futureScore(entry, 0)));
  return <section className="min-w-0 rounded-2xl border border-line bg-white" aria-label="记忆详情">
    <div className="border-b border-line p-5">
      <div className="mb-3 flex flex-wrap items-center gap-2"><span className="memory-badge !bg-indigo-50 !text-indigo-600">{kindLabels[entry.kind] || entry.kind}</span><span className={`memory-badge ${entry.status}`}>{statusLabels[entry.status] || entry.status}</span><span className="ml-auto text-[11px] text-slate-400">v{entry.version}</span></div>
      <h2 className="break-words text-base font-semibold leading-7 text-ink">{entry.title || "未命名记忆"}</h2>
      {entry.key && <p className="mt-1 break-all font-mono text-[11px] text-slate-400">{entry.key}</p>}
      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-slate-600">{entry.content}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" onClick={onEdit} disabled={busy || entry.status === "deleted"} className="memory-button"><Pencil size={13} />编辑</button>
        <button type="button" onClick={onPin} disabled={busy || entry.status === "deleted"} className={`memory-button ${entry.pinned ? "!border-indigo-200 !bg-indigo-50 !text-indigo-600" : ""}`}><Pin size={13} />{entry.pinned ? "取消固定" : "固定"}</button>
        {entry.status === "deleted" ? <button type="button" disabled={busy} onClick={onRestore} className="memory-button"><RotateCcw size={13} />恢复记忆</button> : <button type="button" disabled={busy} onClick={onForget} className="memory-button !text-rose-500"><Trash2 size={13} />遗忘</button>}
      </div>
    </div>
    <div className="p-5">
      <div className="flex items-center justify-between text-xs"><h3 className="font-semibold text-ink">有效记忆分数</h3><span className="font-mono text-indigo-600">{(score * 100).toFixed(1)}%</span></div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-indigo-50"><div className="h-full rounded-full bg-indigo-500" style={{ width: `${score * 100}%` }} /></div>
      <div className="mt-4 grid grid-cols-3 gap-2 text-[11px] text-slate-400"><div>重要度<strong className="mt-1 block text-sm font-medium text-slate-700">{Math.round(entry.importance * 100)}%</strong></div><div>置信度<strong className="mt-1 block text-sm font-medium text-slate-700">{Math.round(entry.confidence * 100)}%</strong></div><div>半衰期<strong className="mt-1 block text-sm font-medium text-slate-700">{entry.half_life_days} 天</strong></div></div>
      <div className="mt-5 rounded-xl border border-line bg-panel p-3">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-slate-600"><Clock3 size={13} />未来分数预估{entry.pinned ? " · 已固定" : ""}</div>
        <div className="mt-4 flex h-20 items-end justify-between gap-3">{[0, 7, 30, 90].map((days) => {
          const value = futureScore(entry, days);
          return <div key={days} className="flex h-full min-w-0 flex-1 flex-col justify-end gap-1 text-center"><span className="text-[9px] tabular-nums text-slate-500">{(value * 100).toFixed(1)}%</span><div className="mx-auto w-full max-w-9 rounded-t bg-indigo-300" style={{ height: `${Math.max(1, value * 48)}px` }} /><span className="text-[9px] text-slate-400">{days ? `${days} 天后` : "现在"}</span></div>;
        })}</div>
        <p className="mt-3 text-[10px] leading-5 text-slate-400">分数 = 重要度 × 置信度 × 2<sup>−经过天数 / 半衰期</sup>。读取不会续期；过期、遗忘或冲突条目不自动召回。</p>
      </div>
      <dl className="mt-5 space-y-3 text-xs"><div className="flex justify-between gap-3"><dt className="text-slate-400">最后确认</dt><dd className="text-right text-slate-600">{dateLabel(entry.last_confirmed_at || entry.created_at)}</dd></div><div className="flex justify-between gap-3"><dt className="text-slate-400">有效期至</dt><dd className="text-right text-slate-600">{entry.expires_at ? dateLabel(entry.expires_at) : "长期有效"}</dd></div><div className="flex justify-between gap-3"><dt className="text-slate-400">最近修改</dt><dd className="text-right text-slate-600">{dateLabel(entry.updated_at)}</dd></div></dl>
      <details className="mt-5 rounded-lg border border-line px-3 py-2"><summary className="cursor-pointer text-xs font-medium text-slate-600">来源与元数据</summary><div className="mt-2 space-y-2"><p className="memory-json">{describeValue(entry.source)}</p><p className="break-all text-[10px] text-slate-400">会话：{entry.conversation_id}</p>{entry.metadata && <pre className="memory-json">{describeValue(entry.metadata)}</pre>}</div></details>
    </div>
    <div className="border-t border-line p-5"><button type="button" onClick={() => setHistoryOpen(!historyOpen)} aria-expanded={historyOpen} className="flex w-full items-center gap-2 text-xs font-semibold text-slate-600"><History size={15} />版本历史<span className="ml-auto text-slate-400">{historyOpen ? "收起" : "展开"}</span></button>
      {historyOpen && <div className="mt-4 space-y-3">{loading && <p className="text-xs text-slate-400">正在读取版本…</p>}{error && <p className="text-xs text-rose-600" role="alert">{error}</p>}{!loading && !error && !revisions.length && <p className="text-xs text-slate-400">暂无版本记录。</p>}{revisions.map((revision) => <div key={revision.version} className="rounded-lg border border-line p-3"><div className="flex flex-wrap items-center gap-2 text-[11px]"><FileClock size={13} className="text-indigo-400" /><span className="font-semibold text-slate-600">v{revision.version}</span><span className="text-slate-400">{revision.operation}</span>{revision.version !== entry.version && <button type="button" disabled={busy} className="ml-auto text-indigo-600 hover:underline disabled:opacity-40" onClick={() => onRevert(revision.version)}>回滚到此版本</button>}</div><p className="mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-slate-500">{revision.snapshot.content || revision.snapshot.title || "此版本无文本快照"}</p><details className="mt-1"><summary className="cursor-pointer text-[10px] text-slate-400">完整快照 · {dateLabel(revision.created_at)}</summary><pre className="memory-json mt-2">{describeValue(revision.snapshot)}</pre></details></div>)}</div>}
    </div>
  </section>;
}
