"use client";

import { useRouter } from "next/navigation";

export default function BrandHeader({ compact = false }: { compact?: boolean }) {
  const router = useRouter();
  return (
    <header className={`topbar ${compact ? "compact" : ""}`}>
      <div className="wrap topbar-inner">
        <button className="brand" type="button" onClick={() => router.push("/")} aria-label="返回 SnapNote 首页">
          <span className="brand-mark">S</span><b>SnapNote</b><em>BETA</em>
        </button>
        <nav aria-label="主导航">
          <button type="button" className="nav-action" onClick={() => router.push("/#upload")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z" />
              <path d="M14 3v6h6M12 12v6M9 15h6" />
            </svg>
            新建笔记
          </button>
          <button type="button" className="nav-action" onClick={() => router.push("/notes")}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="4" y="3" width="16" height="18" rx="2" />
              <path d="M8 3v18M12 8h4M12 12h4M12 16h3" />
            </svg>
            笔记管理
          </button>
          <a className="github-link" href="https://github.com/guyue356/SnapNote" target="_blank" rel="noreferrer" aria-label="在 GitHub 查看 SnapNote">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path fill="currentColor" d="M12 .7A11.5 11.5 0 0 0 8.36 23.1c.58.1.79-.25.79-.56v-2.23c-3.22.7-3.9-1.37-3.9-1.37-.53-1.34-1.29-1.7-1.29-1.7-1.05-.72.08-.7.08-.7 1.16.08 1.78 1.2 1.78 1.2 1.04 1.77 2.72 1.26 3.38.96.1-.75.4-1.26.74-1.55-2.57-.3-5.27-1.29-5.27-5.69 0-1.26.45-2.28 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.16 1.18A10.97 10.97 0 0 1 12 6.11c.98 0 1.94.13 2.84.38 2.2-1.49 3.16-1.18 3.16-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.83 1.19 3.09 0 4.42-2.71 5.39-5.29 5.68.42.36.79 1.07.79 2.16v3.24c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .7Z" />
            </svg>
            GitHub
          </a>
        </nav>
      </div>
    </header>
  );
}
