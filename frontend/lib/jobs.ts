import { API_BASE_URL } from "@/lib/api";

export type BackgroundJob = {
  job_id: string;
  repo_id: string;
  status: "queued" | "running" | "retrying" | "completed" | "failed" | "cancelled";
  attempts: number;
  max_attempts: number;
  result: { synced?: Record<string, number>; warnings?: string[] };
  error_message?: string | null;
  publish_error?: string | null;
};

export function jobProgress(job: BackgroundJob): string {
  if (job.status === "queued") return job.publish_error ? "后台服务暂不可用，任务已保存，等待恢复。" : "任务已入队，等待处理…";
  if (job.status === "running") return "正在同步仓库…";
  if (job.status === "retrying") return `第 ${job.attempts}/${job.max_attempts} 次处理未完成，等待重试：${job.error_message || "临时故障"}`;
  if (job.status === "completed") return job.result.warnings?.length ? `数据同步完成。${job.result.warnings.join(" ")}` : "仓库同步完成。";
  return job.error_message || (job.status === "cancelled" ? "任务已取消。" : "同步失败。");
}

export async function waitForRepoJob(
  repoId: string,
  jobId: string,
  options: { signal?: AbortSignal; onProgress?: (job: BackgroundJob) => void; timeoutMs?: number } = {}
): Promise<BackgroundJob> {
  const deadline = Date.now() + (options.timeoutMs ?? 10 * 60 * 1000);
  while (Date.now() < deadline) {
    if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const timeout = AbortSignal.timeout(15000);
    const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    let response: Response | undefined;
    try {
      response = await fetch(`${API_BASE_URL}/api/repos/${encodeURIComponent(repoId)}/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store", signal });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      // Network outages and per-request timeouts must not discard the durable job.
    }
    if (response && !response.ok && response.status !== 429 && response.status < 500) throw new Error(response.status === 404 ? "任务或仓库已移除。" : `任务状态查询失败（${response.status}）。`);
    if (response?.ok) {
      const job = await response.json() as BackgroundJob;
      if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
      options.onProgress?.(job);
      if (job.status === "completed") return job;
      if (job.status === "failed" || job.status === "cancelled") throw new Error(jobProgress(job));
    }
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
      const timer = setTimeout(() => { options.signal?.removeEventListener("abort", abort); resolve(); }, 1500);
      options.signal?.addEventListener("abort", abort, { once: true });
      if (options.signal?.aborted) abort();
    });
  }
  throw new Error("任务仍在后台处理，可刷新页面继续查看状态。");
}
