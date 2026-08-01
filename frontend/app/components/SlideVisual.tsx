export default function SlideVisual({ index = 0, compact = false }: { index?: number; compact?: boolean }) {
  const themes = ["violet", "mint", "orange", "blue"];
  const titles = ["注意力机制的核心直觉", "Query、Key 与 Value", "缩放点积注意力", "多头注意力与并行表示"];
  return (
    <div className={`slide-visual ${themes[index % themes.length]} ${compact ? "compact" : ""}`}>
      <span className="slide-no">0{index + 1}</span>
      <small>TRANSFORMER · FOUNDATIONS</small>
      <h4>{titles[index % titles.length]}</h4>
      <div className="slide-diagram">
        <i /><b>Q</b><span>×</span><b>K</b><span>→</span><em>Attention</em>
      </div>
      <div className="slide-lines"><i /><i /><i /></div>
    </div>
  );
}
