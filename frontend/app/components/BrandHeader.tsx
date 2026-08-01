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
          <button type="button" onClick={() => router.push("/#recent")}>处理中心</button>
          <a href="https://github.com" target="_blank" rel="noreferrer">使用指南</a>
          <span className="nav-avatar">SN</span>
        </nav>
      </div>
    </header>
  );
}
