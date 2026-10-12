import { Form, Input, InputNumber, Select, Switch } from "antd";
import type { InputSchema } from "../api";

/** Shared typed controls for running a report and editing its saved inputs. */
export function inputField(name: string, prop: InputSchema["properties"][string], required: boolean, prefix?: string, definitions?: InputSchema["$defs"]) {
  const resolve = (field: typeof prop) => ({ ...(field.$ref ? definitions?.[field.$ref.split("/").pop()!] : {}), ...field });
  prop = resolve(prop);
  const alternatives = prop.anyOf?.map(resolve);
  const choices = prop.enum ?? alternatives?.find((a) => a.enum)?.enum;
  const kind = prop.type ?? alternatives?.find((a) => a.type && a.type !== "null")?.type;
  const label = name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const structured = kind === "object" || kind === "array";
  const control = choices ? <Select allowClear={!required} options={choices.map((c) => ({ value: c as string, label: String(c) }))} />
    : kind === "integer" || kind === "number" ? <InputNumber style={{ width: "100%" }} />
    : kind === "boolean" ? <Switch />
    : prop.hidden ? <Input.Password autoComplete="off" />
    : structured ? <Input.TextArea rows={4} /> : <Input />;
  return (
    <Form.Item key={name} name={prefix ? [prefix, name] : name} label={label} tooltip={prop.description}
      initialValue={prefix ? undefined : (prop.default ?? (kind === "boolean" ? false : undefined)) as never}
      valuePropName={kind === "boolean" && !choices ? "checked" : "value"}
      {...(structured ? {
        getValueProps: (value: unknown) => ({ value: typeof value === "string" ? value : JSON.stringify(value, null, 2) }),
        normalize: (value: string) => { try { return JSON.parse(value); } catch { return value; } },
      } : {})}
      rules={[
        ...(required && kind !== "boolean" ? [{ required: true, message: `${label} is required.` }] : []),
        ...(structured ? [{ validator: (_: unknown, value: unknown) => {
          if (value == null || value === "") return Promise.resolve();
          const valid = kind === "array" ? Array.isArray(value) : typeof value === "object" && !Array.isArray(value);
          return valid ? Promise.resolve() : Promise.reject(new Error(`Enter a JSON ${kind}.`));
        } }] : []),
      ]}>{control}</Form.Item>
  );
}
