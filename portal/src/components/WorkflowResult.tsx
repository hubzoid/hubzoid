import { Button, Collapse, Descriptions, Space, Typography } from "antd";
import { prettyOutput } from "../lib/format";
import { resultArtifacts, resultValue } from "../lib/results";

export function WorkflowResult({ output }: { output: string }) {
  const value = resultValue(output);
  const artifacts = resultArtifacts(output);
  const record = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  const artifactRoot = record && typeof record.id === "string" && artifacts.some((a) => a.url === `/portal/artifacts/${record.id}`);
  const fields = value && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value).filter(([key, v]) => (v == null || typeof v !== "object") &&
      (!artifactRoot || ["filename", "size"].includes(key))) : [];
  if (typeof value === "string") return <pre className="output">{value}</pre>;
  return <Space direction="vertical" style={{ width: "100%" }}>
    {artifacts.length > 0 && <Space wrap>{artifacts.map((a) =>
      <Button key={a.url} type="primary" href={a.url} target="_blank" rel="noopener noreferrer">Open {a.title}</Button>,
    )}</Space>}
    {fields.length > 0 && <Descriptions size="small" column={{ xs: 1, sm: 2 }} items={fields.map(([key, v]) => ({
      key, label: key === "size" && artifactRoot ? "Size (bytes)" : key.replaceAll("_", " "), children: <Typography.Text>{String(v ?? "—")}</Typography.Text>,
    }))} />}
    <Collapse items={[{ key: "raw", label: "Full result", children: <pre className="output">{prettyOutput(output)}</pre> }]} />
  </Space>;
}
