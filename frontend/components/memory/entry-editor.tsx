"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Brain, Info, Save, X } from "lucide-react";
import { kindLabels, type Conversation, type EntryDraft, type MemoryEntry, type MemoryKind } from "@/lib/memory";

export type EntryPayload = Omit<EntryDraft, "ttl_days"> & { ttl_days?: number | null };

export function EntryEditor({ entry, conversations, conversationId, saving, error, onReload, onClose, onSubmit }: {
  entry: MemoryEntry | null; conversations: Conversation[]; conversationId: string; saving: boolean; error: string;
  onReload?: () => void; onClose: () => void; onSubmit: (draft: EntryPayload) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState<EntryDraft>({ conversation_id: entry?.conversation_id || conversationId || conversations[0]?.id || "", kind: entry?.kind || "fact", key: entry?.key || "", title: entry?.title || "", content: entry?.content || "", importance: entry?.importance ?? .8, confidence: entry?.confidence ?? .9, pinned: entry?.pinned ?? false, half_life_days: entry?.half_life_days ?? 30, ttl_days: 30 });
  const [ttlMode, setTtlMode] = useState(entry ? "keep" : "forever");
  useEffect(() => { dialog.current?.showModal(); }, []);
  function submit(event: FormEvent) {
    event.preventDefault();
    const { ttl_days, ...fields } = draft;
    onSubmit({ ...fields, title: fields.title.trim(), key: fields.key.trim(), content: fields.content.trim(), ...(ttlMode === "keep" ? {} : { ttl_days: ttlMode === "forever" ? null : ttl_days }) });
  }
  return <dialog ref={dialog} onCancel={(event) => { event.preventDefault(); if (!saving) onClose(); }} className="memory-native-dialog" aria-labelledby="memory-editor-title">
    <form onSubmit={submit} className="memory-dialog">
      <div className="mb-6 flex items-start justify-between gap-4"><div><div className="mb-2 flex items-center gap-2 text-xs font-semibold text-indigo-500"><Brain size={15} /> MEMORY EDITOR</div><h2 id="memory-editor-title" className="text-xl font-semibold text-ink">{entry ? "编辑记忆" : "新增对话记忆"}</h2><p className="mt-1 text-xs text-slate-400">{entry ? `基于版本 v${entry.version} 保存，形成新的确认版本。` : "为当前项目保存一条有来源、可调整的记忆。"}</p></div><button type="button" onClick={onClose} disabled={saving} aria-label="关闭编辑器" className="memory-button !p-2"><X size={16} /></button></div>
      {error && <div role="alert" className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs leading-6 text-rose-700">{error}{onReload && <button type="button" className="mt-2 block font-semibold underline" onClick={onReload}>放弃本地修改并加载最新版本</button>}</div>}
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="memory-label sm:col-span-2">所属会话<select className="memory-field" required disabled={Boolean(entry)} value={draft.conversation_id} onChange={(event) => setDraft({ ...draft, conversation_id: event.target.value })}><option value="">选择会话</option>{conversations.map((conversation) => <option key={conversation.id} value={conversation.id}>{conversation.title}</option>)}</select></label>
        <label className="memory-label">记忆类型<select className="memory-field" value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as MemoryKind })}>{Object.entries(kindLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label className="memory-label">事实键（可选）<input className="memory-field" maxLength={240} value={draft.key} onChange={(event) => setDraft({ ...draft, key: event.target.value })} placeholder="例如 project.database" /></label>
        <label className="memory-label sm:col-span-2">标题（可选）<input autoFocus className="memory-field" maxLength={300} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} placeholder="用一句话概括这条记忆" /></label>
        <label className="memory-label sm:col-span-2">记忆内容<textarea required maxLength={20000} className="memory-field min-h-28 resize-y" value={draft.content} onChange={(event) => setDraft({ ...draft, content: event.target.value })} placeholder="记录可核对的事实、决策或偏好…" /></label>
        <label className="memory-label">重要度 · {Math.round(draft.importance * 100)}%<input type="range" min="0" max="1" step="0.01" className="w-full accent-indigo-600" value={draft.importance} onChange={(event) => setDraft({ ...draft, importance: Number(event.target.value) })} /></label>
        <label className="memory-label">置信度 · {Math.round(draft.confidence * 100)}%<input type="range" min="0" max="1" step="0.01" className="w-full accent-indigo-600" value={draft.confidence} onChange={(event) => setDraft({ ...draft, confidence: Number(event.target.value) })} /></label>
        <label className="memory-label">半衰期（天）<input className="memory-field" type="number" min="0.01" max="36500" step="0.01" required value={draft.half_life_days} onChange={(event) => setDraft({ ...draft, half_life_days: Number(event.target.value) })} /></label>
        <label className="memory-label">有效期<select className="memory-field" value={ttlMode} onChange={(event) => setTtlMode(event.target.value)}>{entry && <option value="keep">保持当前有效期</option>}<option value="forever">长期有效</option><option value="days">从现在起计时</option></select></label>
        {ttlMode === "days" && <label className="memory-label sm:col-span-2">有效天数<input className="memory-field" type="number" min="0.01" max="36500" step="0.01" required value={draft.ttl_days ?? 30} onChange={(event) => setDraft({ ...draft, ttl_days: Number(event.target.value) })} /></label>}
        <label className="flex items-center gap-2 rounded-lg bg-panel p-3 text-xs text-slate-600 sm:col-span-2"><input type="checkbox" checked={draft.pinned} onChange={(event) => setDraft({ ...draft, pinned: event.target.checked })} className="accent-indigo-600" />固定此条记忆，停止时间衰减（设置的有效期仍生效）</label>
      </div>
      <div className="mt-5 flex gap-2 text-xs leading-5 text-slate-400"><Info size={14} className="mt-0.5 shrink-0" />遗忘和到期仅影响记忆召回；聊天原文始终保留。</div>
      <div className="mt-6 flex justify-end gap-2 border-t border-line pt-4"><button type="button" onClick={onClose} disabled={saving} className="memory-button">取消</button><button type="submit" disabled={saving || !draft.content.trim() || !draft.conversation_id} className="memory-button memory-primary"><Save size={14} />{saving ? "正在保存…" : "保存记忆"}</button></div>
    </form>
  </dialog>;
}

export function ForgetDialog({ title, saving, onCancel, onConfirm }: { title: string; saving: boolean; onCancel: () => void; onConfirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} onCancel={(event) => { event.preventDefault(); if (!saving) onCancel(); }} className="memory-native-dialog" aria-labelledby="memory-forget-title"><div className="memory-dialog max-w-md"><h2 id="memory-forget-title" className="text-lg font-semibold text-ink">遗忘这条记忆？</h2><p className="mt-3 break-words text-sm text-slate-600">{title}</p><p className="mt-3 text-xs leading-6 text-slate-400">条目将停止参与自动召回。你可以从「已遗忘」筛选中恢复它，聊天原文与版本记录会保留。</p><div className="mt-6 flex justify-end gap-2"><button autoFocus type="button" onClick={onCancel} disabled={saving} className="memory-button">取消</button><button type="button" onClick={onConfirm} disabled={saving} className="memory-button !border-rose-200 !bg-rose-50 !text-rose-700">{saving ? "正在处理…" : "确认遗忘"}</button></div></div></dialog>;
}
