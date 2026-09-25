import { Tooltip } from "antd";


export function Cost({ usd, unpriced }: { usd: number | null; unpriced: number }) {
  if (usd == null)
    return <Tooltip title={unpriced ? "No model price is known for these calls." : "No model calls in this period."}>—</Tooltip>;
  const value = usd < 0.01 && usd > 0 ? "< $0.01" : `$${usd.toFixed(2)}`;
  return unpriced ? (
    <Tooltip title={`${unpriced.toLocaleString()} calls had no known price and are not included.`}>
      <span>{value}*</span>
    </Tooltip>
  ) : <>{value}</>;
}

