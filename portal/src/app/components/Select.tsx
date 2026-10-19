// Shadcn-style Select composition using the app's existing Radix dependency.
// Keep popup colours under the same theme contract as buttons and Settings.
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Select as Primitive } from "radix-ui";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { cx } from "./ui";

export type SelectOption = { value: string; label: ReactNode; disabled?: boolean };
type Props = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "onChange" | "children"> & {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  required?: boolean;
};

// Encode every value so an optional empty-string choice remains selectable.
// Radix reserves the empty value itself for its unselected/placeholder state.
const encode = (value: string) => `value:${value}`;

export const Select = forwardRef<HTMLButtonElement, Props>(function Select(
  { value, onValueChange, options, placeholder, className, disabled, required, ...trigger }, ref,
) {
  return (
    <Primitive.Root
      value={options.some((option) => option.value === value) ? encode(value) : ""}
      onValueChange={(next) => onValueChange(next.slice("value:".length))}
      disabled={disabled}
      required={required}
    >
      <Primitive.Trigger ref={ref} className={cx("hz-select", className)} {...trigger}>
        <span className="hz-select-value"><Primitive.Value placeholder={placeholder} /></span>
        <Primitive.Icon asChild><ChevronDown size={14} aria-hidden /></Primitive.Icon>
      </Primitive.Trigger>
      <Primitive.Portal>
        <Primitive.Content className="hz-select-content" position="popper" sideOffset={5} collisionPadding={12}>
          <Primitive.ScrollUpButton className="hz-select-scroll"><ChevronUp size={14} aria-hidden /></Primitive.ScrollUpButton>
          <Primitive.Viewport>
            {options.map((option) => (
              <Primitive.Item className="hz-select-item" key={option.value} value={encode(option.value)} disabled={option.disabled}>
                <Primitive.ItemText>{option.label}</Primitive.ItemText>
                <Primitive.ItemIndicator className="hz-select-check"><Check size={14} aria-hidden /></Primitive.ItemIndicator>
              </Primitive.Item>
            ))}
          </Primitive.Viewport>
          <Primitive.ScrollDownButton className="hz-select-scroll"><ChevronDown size={14} aria-hidden /></Primitive.ScrollDownButton>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
});
