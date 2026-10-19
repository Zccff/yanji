import { clsx } from "clsx";

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <section className={clsx("min-w-0 rounded-2xl border border-line bg-white p-5 shadow-sm shadow-slate-200/30", className)}>
      {children}
    </section>
  );
}

export function PageTitle({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6 flex flex-col gap-2 border-b border-line/80 pb-5">
      <div className="text-xs font-medium tracking-[0.18em] text-accent">研迹</div>
      <h1 className="text-2xl font-semibold tracking-normal text-ink md:text-3xl">{title}</h1>
      <p className="max-w-3xl text-sm leading-6 text-slate-600">{description}</p>
    </div>
  );
}

export function PrimaryButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white shadow-sm shadow-indigo-900/10 transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}

export function GhostButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={clsx(
        "w-full rounded-lg border border-line bg-white px-3 py-2 text-left text-sm text-slate-700 transition hover:border-indigo-200 hover:bg-indigo-50/70 hover:text-indigo-900 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
