import { API_BASE_URL } from "@/lib/api";

export type MemoryKind = "fact" | "decision" | "task" | "preference" | "repo_context" | "summary";
export type MemoryEntry = {
  id: string; repo_id: string; conversation_id: string; kind: MemoryKind; key?: string | null;
  title?: string | null; content: string; status: string; version: number;
  importance: number; confidence: number; pinned: boolean; half_life_days: number;
  expires_at?: string | null; created_at?: string | null; updated_at?: string | null;
  last_confirmed_at?: string | null; score: number; source?: unknown; metadata?: Record<string, unknown>;
};
export type MemoryPage = { items: MemoryEntry[]; total: number; stats: Partial<Record<"active" | "expired" | "deleted" | "conflicts" | "total", number>> };
export type MemoryRevision = { version: number; operation: string; snapshot: Partial<MemoryEntry>; created_at?: string };
export type MemoryConflict = { id: string; entry_id: string; current_content: string; proposed_content: string; status: string; created_at?: string; entry_version?: number; version?: number; [key: string]: unknown };
export type MemoryCandidate = { id: string; kind: string; title?: string; content: string; status: string; source?: string; source_type?: string; source_id?: string; conversation_id?: string | null; created_at?: string; metadata?: Record<string, unknown>; [key: string]: unknown };
export type MemoryMessages = { items: Array<{ id: string; role: string; content: string; created_at?: string }>; total: number; search_mode?: string };
export type RuntimeState = { plan?: { goal?: string; status?: string; tasks?: Array<Record<string, unknown>> } | null; agents?: Array<Record<string, unknown>>; pending_actions?: Array<Record<string, unknown>>; enrichment?: { queued?: number; running?: number; failed?: number; jobs?: Array<{ job_id: string; session_id: string; status: string; attempts?: number; error_message?: string | null }> }; updated_at?: string };
export type Conversation = { id: string; title: string; message_count?: number; status?: string };
export type EntryDraft = { conversation_id: string; kind: MemoryKind; key: string; title: string; content: string; importance: number; confidence: number; pinned: boolean; half_life_days: number; ttl_days: number | null };

export const kindLabels: Record<MemoryKind, string> = { fact: "事实", decision: "决策", task: "任务", preference: "偏好", repo_context: "仓库上下文", summary: "摘要" };
export const statusLabels: Record<string, string> = { active: "有效", expired: "已过期", deleted: "已遗忘", conflict: "待解决冲突", pending: "待确认", approved: "已采纳", rejected: "已拒绝", resolved: "已解决", open: "待解决" };

export class MemoryApiError extends Error {
  constructor(public status: number, message: string) { super(status === 409 ? `版本冲突：${message}。请重新加载最新条目后再编辑，当前修改尚未覆盖服务器。` : message); }
}

export async function memoryRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method, cache: "no-store", ...(body !== undefined ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.message;
    const message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `请求失败（${response.status}）`;
    throw new MemoryApiError(response.status, message);
  }
  return payload as T;
}

export function memoryPath(repo: string, suffix: string, params: Record<string, string | number | undefined> = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return `/api/repos/${encodeURIComponent(repo)}/memory/${suffix}${query.size ? `?${query}` : ""}`;
}

export function dateLabel(value?: string | null) {
  if (!value) return "暂无记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", year: "numeric" });
}

export function describeValue(value: unknown): string {
  if (value == null || value === "") return "未提供";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function futureScore(entry: MemoryEntry, days: number, now = Date.now()) {
  if (["deleted", "expired", "conflict"].includes(entry.status)) return 0;
  const expiry = entry.expires_at ? new Date(entry.expires_at).getTime() : Infinity;
  if (now + days * 86400000 >= expiry) return 0;
  const origin = entry.last_confirmed_at || entry.created_at;
  const originMs = origin ? new Date(origin).getTime() : now;
  const age = Number.isFinite(originMs) ? Math.max(0, (now - originMs) / 86400000) : 0;
  const factor = entry.pinned ? 1 : Math.pow(2, -(age + days) / Math.max(.001, entry.half_life_days));
  return Math.max(0, Math.min(1, entry.importance * entry.confidence * factor));
}
