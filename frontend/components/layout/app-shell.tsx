"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { Activity, ArrowUpRight, BookOpen, Brain, Database, FlaskConical, FolderGit2, GitBranch, LayoutDashboard, Menu, Network, PanelTop, X } from "lucide-react";

const navItems = [
  { href: "/", label: "协作工作台", icon: LayoutDashboard, group: "工作空间" },
  { href: "/memory", label: "对话记忆", icon: Brain, group: "工作空间" },
  { href: "/knowledge", label: "项目知识", icon: BookOpen, group: "工作空间" },
  { href: "/rag", label: "RAG 工作室", icon: Database, group: "工作空间" },
  { href: "/repos", label: "代码仓库", icon: FolderGit2, group: "工程管理" },
  { href: "/action-drafts", label: "待确认操作", icon: GitBranch, group: "工程管理" },
  { href: "/workflow-runs", label: "执行记录", icon: Activity, group: "工程管理" },
  { href: "/workspaces", label: "分析报告", icon: PanelTop, group: "工程管理" },
  { href: "/code-graph", label: "代码图谱", icon: Network, group: "工程管理" },
  { href: "/evals", label: "评估实验", icon: FlaskConical, group: "工程管理" }
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const current = navItems.find((item) => item.href === "/" ? pathname === "/" : pathname.startsWith(item.href));
  return (
    <div className="app-frame">
      <header className="app-mobile-header">
        <Link href="/" aria-label="研迹首页" className="flex items-center gap-2 font-semibold text-ink"><span className="brand-symbol text-lg font-bold">研</span>研迹</Link>
        <button type="button" onClick={() => setMenuOpen(!menuOpen)} className="rounded-lg border border-line bg-white p-2 text-slate-600" aria-label={menuOpen ? "关闭导航" : "打开导航"} aria-expanded={menuOpen}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button>
      </header>
      {menuOpen && <button className="app-nav-backdrop" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
      <aside className={`app-navigation ${menuOpen ? "is-open" : ""}`} aria-label="主导航">
        <Link href="/" aria-label="研迹首页" className="app-brand" onClick={() => setMenuOpen(false)}><span className="brand-symbol text-xl font-bold">研</span><span className="nav-label">研迹<small>研发协作工作台</small></span></Link>
        <nav className="flex-1 space-y-6 py-5">
          {["工作空间", "工程管理"].map((group) => <div key={group}><p className="nav-group-label">{group}</p><div className="space-y-1">{navItems.filter((item) => item.group === group).map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return <Link href={href} key={href} onClick={() => setMenuOpen(false)} aria-current={active ? "page" : undefined} className={`app-nav-link ${active ? "is-active" : ""}`} title={label}><Icon size={18} strokeWidth={active ? 2.2 : 1.8} /><span className="nav-label">{label}</span>{active && <span className="nav-active-dot" />}</Link>;
          })}</div></div>)}
        </nav>
        <div className="nav-footer"><span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-indigo-100 text-xs font-bold text-indigo-700">研迹</span><div className="nav-label"><p className="text-xs font-semibold text-ink">研发协作空间</p><p className="mt-1 text-[11px] text-slate-400">项目 · 对话 · 可追溯知识</p></div></div>
      </aside>
      <div className="app-main-column">
        {pathname !== "/" && <div className="app-breadcrumb"><span className="text-slate-400">工作空间</span><span className="text-slate-300">/</span><span>{current?.label || "项目详情"}</span><Link href="/" className="ml-auto inline-flex items-center gap-1 text-slate-500 hover:text-indigo-600">返回协作 <ArrowUpRight size={14} /></Link></div>}
        <main id="main-content" className={`app-content ${pathname === "/" ? "is-workspace" : ""}`}>{children}</main>
      </div>
    </div>
  );
}
