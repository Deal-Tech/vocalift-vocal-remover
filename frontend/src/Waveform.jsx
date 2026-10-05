// Decorative preview drawn locally; no audio or assets are sent to another site.
export default function Waveform({ kind = 'music' }) {
  const vocal = kind === 'vocal';
  return <svg className={`waveform ${kind}`} viewBox="0 0 640 76" preserveAspectRatio="none" aria-hidden="true">
    <path className="wave-baseline" d="M0 38H640" />
    {Array.from({ length: 256 }, (_, i) => {
      const envelope = vocal ? Math.max(0.035, Math.sin(i * 0.073) * Math.cos(i * 0.031)) : 0.35 + Math.abs(Math.sin(i * 0.017)) * 0.55;
      const height = 2 + envelope * (6 + Math.abs(Math.sin(i * 2.31) * Math.cos(i * 0.39)) * 30);
      return <line key={i} x1={i * 2.5} x2={i * 2.5} y1={38 - height} y2={38 + height} />;
    })}
  </svg>;
}
