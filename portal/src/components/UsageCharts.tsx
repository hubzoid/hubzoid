// Small SVG charts for the Usage page: stacked bars, lines and a sparkline.
// Colour follows the brand (HubzoidStudio brand/visual.md): neutrals carry the
// data and brand orange marks the one series that matters or the hovered bar.
// Drawn at the measured width so labels stay sharp; no chart library.
import { useEffect, useRef, useState, type ReactNode } from "react";

export type Series = { key: string; label: string; color: string; values: number[] };

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** A round axis maximum: 1, 2, 2.5 or 5 times a power of ten. */
function niceMax(value: number) {
  if (!(value > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  const m = value / exp;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * exp;
}

const L = 46, B = 22, T = 8;

function Axis({ width, height, max, right, format }: { width: number; height: number; max: number; right: number; format: (n: number) => string }) {
  const y = (v: number) => T + (height - T - B) * (1 - v / max);
  return (
    <>
      {[0, 1, 2, 3, 4].map((i) => {
        const v = (max * i) / 4, yy = Math.round(y(v)) + 0.5;
        return (
          <g key={i}>
            <line className={i ? "hz-chart-grid" : "hz-chart-base"} x1={L} x2={width - right} y1={yy} y2={yy} />
            <text className="hz-chart-axis" x={L - 8} y={yy + 4} textAnchor="end">{format(v)}</text>
          </g>
        );
      })}
    </>
  );
}

function XLabels({ labels, x, width, right, height }: { labels: string[]; x: (i: number) => number; width: number; right: number; height: number }) {
  const n = labels.length;
  const every = Math.ceil(n / Math.max(2, Math.floor((width - L - right) / 64)));
  return (
    <>
      {labels.map((t, i) => ((n - 1 - i) % every === 0
        ? <text key={i} className="hz-chart-axis" x={x(i)} y={height - 5} textAnchor="middle">{t}</text>
        : null))}
    </>
  );
}

function Tip({ at, children }: { at: { x: number; y: number; w: number } | null; children: ReactNode }) {
  if (!at) return null;
  const left = at.x + 180 > at.w ? at.x - 196 : at.x + 14;
  return <div className="hz-chart-tip" style={{ left, top: Math.max(0, at.y - 30) }}>{children}</div>;
}

/** Stacked bars, one per time bucket. Buckets before `missingBefore` are
 *  drawn hatched (not recorded). A single series turns orange on hover. */
export function StackedBars({ labels, series, format, tooltip, missingBefore = 0, height = 230, label }: {
  labels: string[]; series: Series[]; format: (n: number) => string; label: string;
  tooltip: (i: number) => ReactNode; missingBefore?: number; height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const n = labels.length;
  const totals = labels.map((_, i) => series.reduce((s, ser) => s + (ser.values[i] || 0), 0));
  const max = niceMax(Math.max(0, ...totals) * 1.05);
  const R = 4, bw = n ? (width - L - R) / n : 0, gap = Math.max(2, Math.min(bw * 0.28, 18)), w = Math.max(1, bw - gap);
  const ph = height - T - B;
  const single = series.length === 1;
  return (
    <div ref={ref} className="hz-chart" style={{ height }} onMouseLeave={() => setHover(null)}>
      {width > 160 && (
        <svg width={width} height={height} role="img" aria-label={label}>
          <defs>
            <pattern id="hz-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="3" height="6" className="hz-chart-hatch" />
            </pattern>
          </defs>
          <Axis width={width} height={height} max={max} right={R} format={format} />
          {hover && !single && <line className="hz-chart-cross" x1={L + hover.i * bw + bw / 2} x2={L + hover.i * bw + bw / 2} y1={T} y2={height - B} />}
          {labels.map((_, i) => {
            const x = L + i * bw + gap / 2;
            if (i < missingBefore) return <rect key={i} x={x} y={T} width={w} height={ph} fill="url(#hz-hatch)" rx={3} />;
            let base = height - B;
            const parts = series.map((ser) => (ph * (ser.values[i] || 0)) / max);
            const top = parts.reduce((a, b) => a + b, 0);
            if (top <= 0) return null;
            return (
              <g key={i}>
                {series.map((ser, j) => {
                  const h = parts[j];
                  if (h <= 0) return null;
                  const yTop = base - h;
                  const last = series.slice(j + 1).every((_, k) => parts[j + 1 + k] <= 0);
                  const fill = single && hover?.i === i ? "var(--hz-brand)" : ser.color;
                  base = yTop;
                  if (last && h > 4) {
                    return <path key={ser.key} fill={fill}
                      d={`M${x},${yTop + h} V${yTop + 4} Q${x},${yTop} ${x + 4},${yTop} H${x + w - 4} Q${x + w},${yTop} ${x + w},${yTop + 4} V${yTop + h} Z`} />;
                  }
                  return <rect key={ser.key} x={x} y={yTop + (last ? 0 : 2)} width={w} height={Math.max(0, h - (last ? 0 : 2))} fill={fill} />;
                })}
              </g>
            );
          })}
          <XLabels labels={labels} x={(i) => L + i * bw + bw / 2} width={width} right={R} height={height} />
          {labels.map((_, i) => (
            <rect key={i} x={L + i * bw} y={T} width={Math.max(0, bw)} height={ph} fill="transparent"
              onMouseMove={(e) => {
                const box = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
                setHover({ i, x: e.clientX - box.left, y: e.clientY - box.top });
              }} />
          ))}
        </svg>
      )}
      <Tip at={hover ? { x: hover.x, y: hover.y, w: width } : null}>{hover ? tooltip(hover.i) : null}</Tip>
    </div>
  );
}

/** Lines over time buckets; null values leave a gap. Each line ends with a dot
 *  and its name, so identity never rests on colour alone. */
export function Lines({ labels, lines, format, tooltip, height = 180, label }: {
  labels: string[]; lines: { key: string; label: string; color: string; values: (number | null)[] }[];
  format: (n: number) => string; tooltip: (i: number) => ReactNode; height?: number; label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const n = labels.length, R = 40;
  const all = lines.flatMap((l) => l.values.filter((v): v is number => v != null));
  const max = niceMax(Math.max(0, ...all) * 1.05);
  const bw = n ? (width - L - R) / n : 0;
  const x = (i: number) => L + i * bw + bw / 2, y = (v: number) => T + (height - T - B) * (1 - v / max);
  const path = (vals: (number | null)[]) => vals.map((v, i) => (v == null ? "" : `${vals[i - 1] == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join(" ");
  return (
    <div ref={ref} className="hz-chart" style={{ height }} onMouseLeave={() => setHover(null)}>
      {width > 160 && (
        <svg width={width} height={height} role="img" aria-label={label}>
          <Axis width={width} height={height} max={max} right={R} format={format} />
          {hover && <line className="hz-chart-cross" x1={x(hover.i)} x2={x(hover.i)} y1={T} y2={height - B} />}
          {lines.map((l) => <path key={l.key} d={path(l.values)} fill="none" stroke={l.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />)}
          {lines.map((l) => {
            const last = l.values.map((v, i) => [v, i] as const).filter(([v]) => v != null).pop();
            if (!last) return null;
            return (
              <g key={l.key}>
                <circle cx={x(last[1])} cy={y(last[0]!)} r={4} fill={l.color} className="hz-chart-dot" />
                <text className="hz-chart-axis hz-chart-end" x={x(last[1]) + 8} y={y(last[0]!) + 4}>{l.label}</text>
              </g>
            );
          })}
          <XLabels labels={labels} x={x} width={width} right={R} height={height} />
          {labels.map((_, i) => (
            <rect key={i} x={L + i * bw} y={T} width={Math.max(0, bw)} height={height - T - B} fill="transparent"
              onMouseMove={(e) => {
                const box = e.currentTarget.ownerSVGElement!.getBoundingClientRect();
                setHover({ i, x: e.clientX - box.left, y: e.clientY - box.top });
              }} />
          ))}
        </svg>
      )}
      <Tip at={hover ? { x: hover.x, y: hover.y, w: width } : null}>{hover ? tooltip(hover.i) : null}</Tip>
    </div>
  );
}

/** A quiet trend line for a total. */
export function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const max = Math.max(...values) || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 54 + 1},${21 - (v / max) * 18}`).join(" ");
  return (
    <svg className="hz-usage-spark" viewBox="0 0 56 22" aria-hidden>
      <polyline points={pts} fill="none" stroke="var(--hz-viz-spark)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
