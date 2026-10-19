"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { AlertCircle, ArrowDownWideNarrow, ArrowLeft, ArrowRight, Brain, Check, ChevronRight, Clock3, Database, GitCompareArrows, History, Layers3, MessageSquareText, Pin, Plus, RefreshCw, Search, ShieldCheck, Activity, X } from "lucide-react";
import type { Repository } from "@/lib/types";
import { dateLabel, kindLabels, MemoryApiError, memoryPath, memoryRequest, statusLabels, type Conversation, type MemoryCandidate, type MemoryConflict, type MemoryEntry, type MemoryMessages, type MemoryPage, type RuntimeState } from "@/lib/memory";
import { EntryEditor, ForgetDialog, type EntryPayload } from "@/components/memory/entry-editor";
import { EntryDetail } from "@/components/memory/entry-detail";
import { CandidatePanel, ConflictPanel, EmptyPanel, MessagesPanel, RuntimePanel } from "@/components/memory/workbench-panels";

const tabs = [
  { id: "entries", label: "记忆条目", icon: Layers3 },
  { id: "conflicts", label: "冲突与版本", icon: GitCompareArrows },
  { id: "messages", label: "聊天原文", icon: MessageSquareText },
  { id: "candidates", label: "待确认知识", icon: ShieldCheck },
  { id: "runtime", label: "执行状态", icon: Activity }
] as const;
type Tab = typeof tabs[number]["id"];
const pageSize = 12;
const emptyPage: MemoryPage = { items: [], total: 0, stats: {} };

export function MemoryWorkbench() {
  const params = useSearchParams();
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [scopeLoading, setScopeLoading] = useState(true);
  const [reposLoading, setReposLoading] = useState(true);
  const [reposError, setReposError] = useState("");
  const [scopeError, setScopeError] = useState("");
  const [scopeReload, setScopeReload] = useState(0);
  const [loadedRepoId, setLoadedRepoId] = useState("");
  const [tab, setTab] = useState<Tab>("entries");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("active");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<MemoryPage>(emptyPage);
  const [conflicts, setConflicts] = useState<MemoryConflict[]>([]);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [messages, setMessages] = useState<MemoryMessages | null>(null);
  const [runtime, setRuntime] = useState<RuntimeState | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [focusedEntry, setFocusedEntry] = useState<MemoryEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState("");
  const [conflictError, setConflictError] = useState(false);
  const [notice, setNotice] = useState("");
  const [editor, setEditor] = useState<{ entry: MemoryEntry | null } | null>(null);
  const [forgetEntry, setForgetEntry] = useState<MemoryEntry | null>(null);
  const selected = focusedEntry || data.items.find((item) => item.id === selectedId) || data.items[0];
  const selectedRepo = repos.find((repo) => repo.id === repoId);
  const selectedConversation = conversations.find((item) => item.id === conversationId);

  async function loadRepos() {
    setReposLoading(true); setReposError("");
    try {
      const rows = await memoryRequest<Repository[]>("/api/repos");
      setRepos(rows);
      setRepoId((current) => rows.some((repo) => repo.id === current) ? current : rows.find((repo) => repo.id === params.get("repo"))?.id || rows[0]?.id || "");
      if (!rows.length) setScopeLoading(false);
    } catch (reason) { setReposError(errorText(reason)); setScopeLoading(false); }
    finally { setReposLoading(false); }
  }
  useEffect(() => { void loadRepos(); /* initial URL selects the incoming workspace scope */ }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!repoId) return;
    let cancelled = false;
    setScopeLoading(true); setScopeError(""); setConversations([]); setConversationId("");
    setData(emptyPage); setSelectedId(""); setFocusedEntry(null); setMessages(null); setRuntime(null); setConflicts([]); setCandidates([]); setOffset(0); setEditor(null); setMutationError("");
    memoryRequest<Conversation[]>(`/api/repos/${encodeURIComponent(repoId)}/conversations`).then((rows) => {
      if (cancelled) return;
      setConversations(rows);
      setConversationId(rows.some((row) => row.id === params.get("conversation")) ? params.get("conversation")! : "");
    }).catch((reason) => { if (!cancelled) setScopeError(errorText(reason)); }).finally(() => { if (!cancelled) { setScopeLoading(false); setLoadedRepoId(repoId); } });
    return () => { cancelled = true; };
  }, [repoId, scopeReload]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const timer = window.setTimeout(() => { setQuery(queryInput.trim()); setOffset(0); }, 300); return () => window.clearTimeout(timer); }, [queryInput]);

  useEffect(() => {
    if (!repoId || scopeLoading || loadedRepoId !== repoId) return;
    let cancelled = false;
    setLoading(true); setErrors({});
    const scoped = { conversation_id: conversationId || undefined };
    const requests: Promise<unknown>[] = [];
    function fetchPanel<T>(name: string, path: string, apply: (value: T) => void) {
      requests.push(memoryRequest<T>(path).then((value) => { if (!cancelled) apply(value); }).catch((reason) => { if (!cancelled) setErrors((current) => ({ ...current, [name]: errorText(reason) })); }));
    }
    fetchPanel<MemoryPage>("entries", memoryPath(repoId, "entries", { ...scoped, q: tab === "entries" ? query : undefined, status: tab === "entries" ? status : "all", kind: tab === "entries" ? kind : undefined, limit: pageSize, offset: tab === "entries" ? offset : 0 }), (value) => {
      setData(value); setSelectedId((id) => value.items.some((item) => item.id === id) ? id : value.items[0]?.id || "");
      if (tab === "entries" && offset > 0 && offset >= value.total) setOffset(Math.max(0, Math.floor((value.total - 1) / pageSize) * pageSize));
    });
    if (tab === "conflicts") fetchPanel<MemoryConflict[]>("conflicts", memoryPath(repoId, "conflicts", scoped), setConflicts);
    if (tab === "messages" && conversationId) fetchPanel<MemoryMessages>("messages", memoryPath(repoId, "messages", { ...scoped, q: query, limit: pageSize, offset }), setMessages);
    if (tab === "candidates") fetchPanel<MemoryCandidate[]>("candidates", memoryPath(repoId, "candidates", { status: "all", limit: 100 }), setCandidates);
    if (tab === "runtime" && conversationId) fetchPanel<RuntimeState>("runtime", memoryPath(repoId, "runtime", scoped), setRuntime);
    Promise.all(requests).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [repoId, conversationId, scopeLoading, loadedRepoId, tab, query, status, kind, offset, reload]);

  function chooseTab(next: Tab) { setTab(next); setOffset(0); setFocusedEntry(null); }
  function refresh() { setFocusedEntry(null); setReload((value) => value + 1); setMutationError(""); setConflictError(false); }
  async function mutate(action: () => Promise<unknown>, message: string | ((result: unknown) => string), closeEditor = false) {
    setBusy(true); setMutationError(""); setConflictError(false); setNotice("");
    try {
      const result = await action();
      if (result && typeof result === "object" && "id" in result && "content" in result) setSelectedId(String(result.id));
      setNotice(typeof message === 'function' ? message(result) : message);
      if (closeEditor) setEditor(null);
      setForgetEntry(null); setFocusedEntry(null); setReload((value) => value + 1);
    } catch (reason) {
      setMutationError(errorText(reason)); setConflictError(reason instanceof MemoryApiError && reason.status === 409);
      setForgetEntry(null);
    } finally { setBusy(false); }
  }
  function saveEntry(draft: EntryPayload) {
    const entry = editor?.entry;
    const { conversation_id, ...fields } = draft;
    void mutate(() => memoryRequest(memoryPath(repoId, entry ? `entries/${entry.id}` : "entries"), entry ? "PATCH" : "POST", entry ? { ...fields, expected_version: entry.version } : { ...fields, conversation_id }), entry ? "记忆已更新，并保存为新版本。" : "记忆已创建。", true);
  }
  function retryEnrichment(sessionId: string) {
    void mutate(() => memoryRequest(memoryPath(repoId, `sessions/${sessionId}/retry`), "POST", {}), (result) => {
      const status = result && typeof result === "object" && "status" in result ? String(result.status) : "queued";
      return status === "completed" ? "本轮记忆已提取完成，无需再次提取。" :
        status === "running" ? "记忆提取任务正在执行，可刷新查看结果。" :
        status === "retrying" ? "记忆提取任务正在等待重试。" : "记忆提取任务已入队，可刷新查看执行状态。";
    });
  }
  async function currentConflictEntry(conflict: MemoryConflict) {
    // A backend-provided current version avoids assuming a filtered page contains the entry.
    if (typeof conflict.entry_version === "number") return conflict.entry_version;
    const entry = await findEntry(conflict.entry_id, conflict.current_content);
    if (!entry) throw new Error("无法获取冲突条目的当前版本，请重新加载冲突列表。");
    return entry.version;
  }
  async function findEntry(id: string, hint?: string) {
    // Search text is bounded so long memory contents do not overflow URL/header limits.
    // Fall back to scoped pages when a pending conflict's content has since changed.
    for (const q of hint ? [hint.slice(0, 120), ""] : [""]) {
      let pageOffset = 0;
      do {
        const page = await memoryRequest<MemoryPage>(memoryPath(repoId, "entries", { status: "all", q, conversation_id: conversationId || undefined, limit: 200, offset: pageOffset }));
        const found = page.items.find((item) => item.id === id);
        if (found) return found;
        pageOffset += page.items.length;
        if (!page.items.length || pageOffset >= page.total) break;
      } while (true);
    }
    return undefined;
  }
  function resolveConflict(conflict: MemoryConflict, resolution: "keep" | "replace") {
    void mutate(async () => memoryRequest(memoryPath(repoId, `conflicts/${conflict.id}/resolve`), "POST", { resolution, expected_version: await currentConflictEntry(conflict) }), resolution === "keep" ? "已保留当前记忆，冲突已解决。" : "已采纳新提议，并保存新的记忆版本。");
  }
  async function openConflictEntry(id: string) {
    const conflict = conflicts.find((item) => item.entry_id === id);
    setBusy(true); setMutationError("");
    try {
      const entry = await findEntry(id, conflict?.current_content);
      if (!entry) throw new Error("条目未找到，请刷新后重试。");
      setTab("entries"); setStatus("all"); setOffset(0); setFocusedEntry(entry); setSelectedId(id);
    } catch (reason) { setMutationError(errorText(reason)); }
    finally { setBusy(false); }
  }
  const candidateMatches = candidates.filter((item) => {
    const metadataConversation = item.conversation_id || item.metadata?.conversation_id;
    const matchesScope = !conversationId || !metadataConversation || metadataConversation === conversationId;
    return matchesScope && (!query || `${item.title || ""} ${item.content}`.toLowerCase().includes(query.toLowerCase()));
  });
  const total = tab === "messages" ? (conversationId ? messages?.total || 0 : 0) : tab === "candidates" ? candidateMatches.length : tab === "conflicts" ? conflicts.length : data.total;
  const paginated = tab !== "runtime";
  const hasTabError = Boolean(errors[tab]);

  return <div className="memory-workbench">
    <header className="mb-7 flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between"><div><div className="mb-3 flex items-center gap-2 text-[10px] font-semibold tracking-[.17em] text-indigo-500"><Brain size={15} /> CONTEXT & MEMORY</div><h1 className="text-2xl font-semibold tracking-tight text-ink lg:text-[28px]">对话记忆工作台</h1><p className="mt-2 max-w-xl text-xs leading-6 text-slate-400">让项目上下文保持准确、适时、可追溯。管理记忆的来源、有效期与版本，保留完整聊天原文。</p></div><div className="flex shrink-0 gap-2"><button className="memory-button !h-9" type="button" onClick={() => void loadRepos()} disabled={busy || reposLoading} title="刷新仓库列表"><Database size={14} /></button><button className="memory-button !h-9" type="button" onClick={refresh} disabled={busy || loading || !repoId}><RefreshCw size={14} className={loading ? "animate-spin" : ""} />刷新</button><button className="memory-button memory-primary !h-9" type="button" disabled={!repoId || scopeLoading || !conversations.length || busy} onClick={() => { setMutationError(""); setEditor({ entry: null }); }}><Plus size={15} />新增记忆</button></div></header>

    <div className="mb-5 flex flex-col gap-3 rounded-2xl border border-line bg-white p-4 sm:flex-row sm:items-center"><div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-indigo-50 text-indigo-500"><Database size={17} /></div><label className="memory-label flex-1">项目仓库<select className="memory-field !border-0 !bg-panel" value={repoId} onChange={(event) => { setRepoId(event.target.value); setNotice(""); }} disabled={busy || Boolean(editor) || reposLoading} aria-label="项目仓库"><option value="">{reposLoading ? "正在读取仓库…" : "选择仓库"}</option>{repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name || repo.name}</option>)}</select></label><ChevronRight size={16} className="hidden shrink-0 text-slate-300 sm:block" /><label className="memory-label flex-1">会话范围<select className="memory-field !border-0 !bg-panel" value={conversationId} onChange={(event) => { setConversationId(event.target.value); setOffset(0); setSelectedId(""); setFocusedEntry(null); setNotice(""); }} disabled={busy || Boolean(editor) || scopeLoading || !repoId} aria-label="会话范围"><option value="">全部会话</option>{conversations.map((conversation) => <option key={conversation.id} value={conversation.id}>{conversation.title}{conversation.message_count == null ? "" : ` · ${conversation.message_count} 条消息`}</option>)}</select></label></div>
    {reposError && <ErrorBanner message={`仓库加载失败：${reposError}`} onRetry={() => void loadRepos()} />}
    {scopeError && <ErrorBanner message={`会话加载失败：${scopeError}`} onRetry={() => setScopeReload((value) => value + 1)} />}
    {mutationError && !editor && <ErrorBanner message={mutationError} onRetry={conflictError ? refresh : undefined} retryLabel="加载最新版本" />}
    {notice && <div role="status" className="mb-5 flex items-center gap-2 rounded-xl border border-emerald-100 bg-emerald-50 px-4 py-3 text-xs text-emerald-700"><Check size={15} /><span className="flex-1">{notice}</span><button type="button" onClick={() => setNotice("")} aria-label="关闭提示"><X size={14} /></button></div>}

    <div className="mb-6 grid grid-cols-2 gap-3 xl:grid-cols-4">{([
      { label: "有效记忆", field: "active", icon: Brain, color: "text-indigo-500 bg-indigo-50" },
      { label: "已到期", field: "expired", icon: Clock3, color: "text-amber-500 bg-amber-50" },
      { label: "待解决冲突", field: "conflicts", icon: GitCompareArrows, color: "text-rose-400 bg-rose-50" },
      { label: "已遗忘", field: "deleted", icon: History, color: "text-slate-400 bg-slate-100" }
    ] as const).map(({ label, field, icon: Icon, color }) => <div key={field} className="flex items-center gap-3 rounded-xl border border-line bg-white p-4"><span className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${color}`}><Icon size={17} strokeWidth={1.7} /></span><div><p className="text-[10px] text-slate-400">{label}</p><p className="mt-1 text-xl font-semibold tabular-nums text-ink">{data.stats[field] ?? "—"}</p></div></div>)}</div>

    <div className="mb-5 flex gap-6 overflow-x-auto border-b border-line" role="tablist" aria-label="记忆管理视图">{tabs.map(({ id, label, icon: Icon }) => <button type="button" key={id} role="tab" id={`tab-${id}`} aria-controls="memory-tabpanel" aria-selected={tab === id} onClick={() => chooseTab(id)} className={`flex shrink-0 items-center gap-2 border-b-2 pb-3.5 pt-1 text-xs transition ${tab === id ? "border-indigo-500 font-semibold text-indigo-600" : "border-transparent text-slate-400 hover:text-indigo-600"}`}><Icon size={15} />{label}</button>)}</div>

    {!["runtime", "conflicts"].includes(tab) && <div className="mb-5 flex flex-col gap-3 sm:flex-row"><label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-line bg-white px-3 py-2.5"><Search size={16} className="shrink-0 text-slate-400" /><input value={queryInput} onChange={(event) => setQueryInput(event.target.value)} className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-slate-400" aria-label={tab === "messages" ? "检索聊天原文" : "搜索记忆"} placeholder={tab === "messages" ? "检索历史消息，支持分页…" : "搜索标题或记忆内容…"} />{queryInput && <button type="button" onClick={() => setQueryInput("")} aria-label="清除搜索"><X size={13} /></button>}</label>{tab === "entries" && <div className="flex min-w-0 gap-2"><select value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); setFocusedEntry(null); }} aria-label="记忆状态" className="memory-field !w-auto flex-1"><option value="active">有效记忆</option><option value="all">全部状态</option><option value="expired">已过期</option><option value="deleted">已遗忘</option><option value="conflict">待解决冲突</option></select><select className="memory-field !w-auto flex-1" aria-label="记忆类型筛选" value={kind} onChange={(event) => { setKind(event.target.value); setOffset(0); setFocusedEntry(null); }}><option value="">全部类型</option>{Object.entries(kindLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div>}</div>}

    <div className="mb-4 flex flex-wrap items-center gap-2 text-[10px] text-slate-400"><span>{selectedRepo?.full_name || "未选择仓库"}</span><ChevronRight size={11} /><span>{selectedConversation?.title || "全部会话"}</span>{tab === "entries" && <span className="ml-auto flex items-center gap-1"><ArrowDownWideNarrow size={12} />共 {data.total} 条</span>}{tab === "candidates" && <span className="ml-auto">仓库候选知识 · {candidateMatches.length} 条</span>}</div>
    {Object.entries(errors).map(([name, message]) => <ErrorBanner key={name} message={`${name === "entries" ? "记忆条目" : tabs.find((item) => item.id === name)?.label || name}加载失败：${message}`} onRetry={refresh} />)}

    <section id="memory-tabpanel" role="tabpanel" aria-labelledby={`tab-${tab}`} aria-busy={loading || scopeLoading} className="min-w-0">
      {reposLoading || scopeLoading || loading ? <div className="memory-empty !min-h-72"><RefreshCw size={24} className="animate-spin text-indigo-300" /><span>正在读取{tabs.find((item) => item.id === tab)?.label}…</span></div> : !repoId ? <EmptyPanel title="先连接一个项目仓库" description="记忆以仓库和会话为边界。前往协作工作台连接项目后，在这里管理真实上下文。" /> : !hasTabError && <>
        {tab === "entries" && (data.items.length || focusedEntry ? <div className="memory-detail-grid"><div className="min-w-0 space-y-3">{focusedEntry && !data.items.some((item) => item.id === focusedEntry.id) && <p className="rounded-lg bg-indigo-50 p-3 text-xs text-indigo-600">正在查看冲突关联条目。点击其他条目可返回当前列表。</p>}{data.items.map((entry) => <button type="button" key={entry.id} onClick={() => { setSelectedId(entry.id); setFocusedEntry(null); }} className={`block w-full rounded-xl border p-4 text-left transition ${selected?.id === entry.id ? "border-indigo-300 bg-white shadow-sm shadow-indigo-100" : "border-line bg-white hover:border-indigo-200"}`}><div className="mb-2.5 flex flex-wrap items-center gap-1.5"><span className="memory-badge !bg-indigo-50 !text-indigo-500">{kindLabels[entry.kind] || entry.kind}</span><span className={`memory-badge ${entry.status}`}>{statusLabels[entry.status] || entry.status}</span>{entry.pinned && <Pin size={11} className="text-indigo-400" />}<span className="ml-auto font-mono text-[10px] text-slate-300">v{entry.version}</span></div><h3 className="truncate text-sm font-semibold text-ink">{entry.title || entry.key || "未命名记忆"}</h3><p className="mt-2 line-clamp-2 break-words text-xs leading-6 text-slate-500">{entry.content}</p><div className="mt-4 flex items-center justify-between gap-2 border-t border-slate-50 pt-3 text-[10px] text-slate-400"><span>{dateLabel(entry.last_confirmed_at || entry.created_at)}</span><span className="flex items-center gap-2"><span className="h-1 w-12 overflow-hidden rounded-full bg-indigo-50"><span className="block h-full rounded-full bg-indigo-300" style={{ width: `${Math.max(0, Math.min(100, (entry.score || 0) * 100))}%` }} /></span>{((entry.score || 0) * 100).toFixed(0)}%</span></div></button>)}</div>{selected && <EntryDetail key={selected.id} entry={selected} repoId={repoId} busy={busy} onEdit={() => { setMutationError(""); setEditor({ entry: selected }); }} onForget={() => setForgetEntry(selected)} onRestore={() => void mutate(() => memoryRequest(memoryPath(repoId, `entries/${selected.id}/restore`), "POST", { expected_version: selected.version }), "记忆已恢复，仍按当前有效期判断是否召回。")} onPin={() => void mutate(() => memoryRequest(memoryPath(repoId, `entries/${selected.id}`), "PATCH", { expected_version: selected.version, pinned: !selected.pinned }), selected.pinned ? "已取消固定。" : "记忆已固定，显式有效期仍生效。")} onRevert={(version) => void mutate(() => memoryRequest(memoryPath(repoId, `entries/${selected.id}/revert`), "POST", { expected_version: selected.version, target_version: version }), `已回滚到 v${version} 的内容，并生成新版本。`)} />}</div> : <EmptyPanel title={query || kind ? "没有匹配的记忆" : status === "deleted" ? "还没有已遗忘的记忆" : "当前范围还没有记忆条目"} description={query || kind ? "调整搜索词或筛选条件，查看其他会话中的记忆。" : "点击「新增记忆」保存一条事实、决策或偏好；对话提取的记忆也会在这里出现。"} />)}
        {tab === "conflicts" && <><div className="mb-4 flex items-center gap-2 rounded-xl border border-indigo-100 bg-indigo-50/40 p-4 text-xs leading-6 text-indigo-600"><History size={16} className="shrink-0" /><span>每次确认都会形成版本。打开关联条目，可在详情底部查看历史并回滚。</span></div><ConflictPanel conflicts={conflicts.slice(offset, offset + pageSize)} busy={busy} onResolve={resolveConflict} onSelectEntry={(id) => void openConflictEntry(id)} /></>}
        {tab === "messages" && (conversationId ? <MessagesPanel data={messages} /> : <EmptyPanel icon="chat" title="选择一个会话查看聊天原文" description="请在上方「会话范围」中选择具体会话，再检索和分页查看原始消息。" />)}
        {tab === "candidates" && <><p className="mb-4 text-[11px] leading-6 text-slate-400">显示当前仓库最近 100 条候选。已有会话来源的候选按所选会话筛选；未标注会话的仓库候选仍会显示。</p><CandidatePanel candidates={candidateMatches.slice(offset, offset + pageSize)} busy={busy} onAction={(id, action) => void mutate(() => memoryRequest(memoryPath(repoId, `candidates/${id}/${action}`), "POST", {}), action === "assess" ? "质量校验已更新，请查看来源与检查项。" : action === "approve" ? "候选知识已确认采纳。" : "候选知识已拒绝。仍保留来源记录。")} /></>}
        {tab === "runtime" && (conversationId ? <RuntimePanel data={runtime} busy={busy} onRetry={retryEnrichment} /> : <EmptyPanel icon="runtime" title="选择一个会话查看执行状态" description="请在上方「会话范围」中选择具体会话，查看该会话的当前计划、任务和记忆提取状态。" />)}
      </>}
    </section>
    {paginated && total > 0 && !loading && !scopeLoading && !hasTabError && <div className="mt-5 flex items-center justify-between gap-3 text-[11px] text-slate-400"><span>{offset + 1}–{Math.min(offset + pageSize, total)} / {total}</span><div className="flex gap-2"><button type="button" className="memory-button" disabled={offset === 0 || busy} onClick={() => { setOffset(Math.max(0, offset - pageSize)); setFocusedEntry(null); }}><ArrowLeft size={13} />上一页</button><button type="button" className="memory-button" disabled={offset + pageSize >= total || busy} onClick={() => { setOffset(offset + pageSize); setFocusedEntry(null); }}>下一页<ArrowRight size={13} /></button></div></div>}
    <footer className="mt-8 flex flex-wrap items-center gap-2 border-t border-line pt-4 text-[10px] leading-6 text-slate-400"><ShieldCheck size={13} /><span>记忆读取不会刷新寿命 · 遗忘与到期保留原始聊天 · 编辑使用版本校验</span><Link href="/" className="ml-auto text-indigo-500 hover:underline">返回协作工作台 →</Link></footer>
    {editor && <EntryEditor entry={editor.entry} conversations={conversations} conversationId={conversationId} saving={busy} error={mutationError} onReload={conflictError ? () => { setEditor(null); refresh(); } : undefined} onClose={() => { setEditor(null); setMutationError(""); }} onSubmit={saveEntry} />}
    {forgetEntry && <ForgetDialog title={forgetEntry.title || forgetEntry.content.slice(0, 80)} saving={busy} onCancel={() => setForgetEntry(null)} onConfirm={() => void mutate(() => memoryRequest(memoryPath(repoId, `entries/${forgetEntry.id}`, { expected_version: forgetEntry.version }), "DELETE"), "记忆已遗忘。可在「已遗忘」筛选中恢复，聊天原文保持完整。")} />}
  </div>;
}

function errorText(reason: unknown) { return reason instanceof Error ? reason.message : String(reason); }
function ErrorBanner({ message, onRetry, retryLabel = "重试" }: { message: string; onRetry?: () => void; retryLabel?: string }) {
  return <div role="alert" className="mb-4 flex flex-wrap items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs leading-6 text-rose-700"><AlertCircle size={15} className="mt-1 shrink-0" /><p className="min-w-0 flex-1 break-words">{message}</p>{onRetry && <button type="button" onClick={onRetry} className="shrink-0 font-semibold underline">{retryLabel}</button>}</div>;
}




