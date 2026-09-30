// Small building blocks shared by every screen of the chat app. Dialogs, menus
// and tooltips come from Radix (already a dependency of assistant-ui): focus
// trapping, Escape to close and keyboard navigation for free.
import {
  forwardRef,
  useId,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { Dialog as RDialog, Tooltip as RTooltip } from "radix-ui";
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";
import { t } from "../i18n/en";
import { displayName, initials } from "../lib/format";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  icon?: ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("hz-btn", `hz-btn-${variant}`, size !== "md" && `hz-btn-${size}`, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Loader2 size={16} className="hz-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
});

type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  size?: "md" | "lg";
  tooltip?: boolean;
};

/** An icon-only button. `label` is its accessible name (and tooltip). */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = "md", tooltip = true, className, children, type = "button", ...rest },
  ref,
) {
  const button = (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      data-size={size}
      className={cx("hz-icon-btn", className)}
      {...rest}
    >
      {children}
    </button>
  );
  if (!tooltip) return button;
  return <Tip label={label}>{button}</Tip>;
});

export function Tip({ label, children, side = "top" }: { label: string; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <RTooltip.Root delayDuration={400}>
      <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content
          side={side}
          sideOffset={6}
          className="z-[70] rounded-md bg-ink px-2 py-1 text-xs font-medium text-bg"
        >
          {label}
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}

export const TooltipProvider = RTooltip.Provider;

export function Spinner({ size = 18, label = t.loading, className }: { size?: number; label?: string; className?: string }) {
  return (
    <span role="status" className={cx("inline-flex items-center text-mute", className)}>
      <Loader2 size={size} className="hz-spin" aria-hidden />
      <span className="sr-only">{label}</span>
    </span>
  );
}

export function PageSpinner() {
  return (
    <div className="flex h-full min-h-[40vh] w-full items-center justify-center">
      <Spinner size={22} />
    </div>
  );
}

type NoticeTone = "info" | "success" | "warning" | "error";
const toneStyle: Record<NoticeTone, string> = {
  info: "bg-info-soft text-info",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  error: "bg-danger-soft text-danger",
};
const toneIcon: Record<NoticeTone, ReactNode> = {
  info: <Info size={16} aria-hidden />,
  success: <CheckCircle2 size={16} aria-hidden />,
  warning: <AlertTriangle size={16} aria-hidden />,
  error: <XCircle size={16} aria-hidden />,
};

/** An inline message. Errors are announced (role=alert), others politely. */
export function Notice({
  tone = "info",
  title,
  children,
  action,
  className,
  onDismiss,
}: {
  tone?: NoticeTone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
  onDismiss?: () => void;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cx("flex items-start gap-3 rounded-[10px] px-3.5 py-3 text-sm", toneStyle[tone], className)}
    >
      <span className="mt-0.5 flex-none">{toneIcon[tone]}</span>
      <div className="min-w-0 flex-1 leading-relaxed">
        {title && <div className="font-semibold">{title}</div>}
        {children && <div className={cx(title ? "mt-0.5" : "", "text-[13.5px]")}>{children}</div>}
        {action && <div className="mt-2.5">{action}</div>}
      </div>
      {onDismiss && (
        <button type="button" className="-m-1 rounded p-1 opacity-70 hover:opacity-100" aria-label={t.close} onClick={onDismiss}>
          <X size={14} aria-hidden />
        </button>
      )}
    </div>
  );
}

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  help?: ReactNode;
  error?: string | null;
};

export const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { label, help, error, id, className, ...rest },
  ref,
) {
  const auto = useId();
  const inputId = id || auto;
  const helpId = `${inputId}-help`;
  const errorId = `${inputId}-error`;
  const describedBy = [error ? errorId : null, help ? helpId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className={className}>
      <label className="hz-label" htmlFor={inputId}>
        {label}
      </label>
      <input
        ref={ref}
        id={inputId}
        className="hz-input"
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...rest}
      />
      {error ? (
        <p id={errorId} className="hz-field-error">
          {error}
        </p>
      ) : help ? (
        <p id={helpId} className="hz-help">
          {help}
        </p>
      ) : null}
    </div>
  );
});

/** Agent picture, or its initials on a neutral tile. */
export function AgentAvatar({
  name,
  src,
  size = 32,
  className,
}: {
  name: string;
  src?: string | null;
  size?: number;
  className?: string;
}) {
  const radius = Math.round(size * 0.28);
  if (src)
    return (
      <img
        src={src}
        alt=""
        width={size}
        height={size}
        className={cx("flex-none border border-line bg-raised object-cover", className)}
        style={{ width: size, height: size, borderRadius: radius }}
      />
    );
  return (
    <span
      aria-hidden
      className={cx(
        "inline-flex flex-none items-center justify-center border border-line bg-sunken font-mono font-semibold text-ink",
        className,
      )}
      style={{ width: size, height: size, borderRadius: radius, fontSize: Math.max(10, Math.round(size * 0.38)) }}
    >
      {initials(displayName(name))}
    </span>
  );
}

export function UserAvatar({ name, size = 28 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex flex-none items-center justify-center rounded-full bg-accent-soft font-semibold text-accent-text"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.4) }}
    >
      {initials(name)}
    </span>
  );
}

/** The Hubzoid mark as type: an orange slash and the name. */
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cx("hz-wordmark", className)} role="img" aria-label={t.product}>
      <span className="slash" aria-hidden>
        /
      </span>
      <span aria-hidden>hubzoid</span>
    </span>
  );
}

/** Confirmation dialog for destructive actions. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel,
  onConfirm,
  busy,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  busy?: boolean;
  error?: string | null;
}) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title={title} role="alertdialog">
      <div className="text-[14.5px] leading-relaxed text-body">{body}</div>
      {error && (
        <Notice tone="error" className="mt-4">
          {error}
        </Notice>
      )}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <RDialog.Close asChild>
          <Button variant="secondary">{t.cancel}</Button>
        </RDialog.Close>
        <Button variant="danger" loading={busy} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  role = "dialog",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  role?: "dialog" | "alertdialog";
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="hz-overlay" />
        <RDialog.Content className="hz-dialog" role={role}>
          <div className="mb-3 flex items-start justify-between gap-4">
            <RDialog.Title className="m-0 text-lg font-semibold tracking-tight text-ink">{title}</RDialog.Title>
            <RDialog.Close asChild>
              <button type="button" className="hz-icon-btn -mr-2 -mt-1" aria-label={t.close}>
                <X size={18} aria-hidden />
              </button>
            </RDialog.Close>
          </div>
          {description ? (
            <RDialog.Description className="mb-4 text-sm leading-relaxed text-mute">{description}</RDialog.Description>
          ) : (
            <RDialog.Description className="sr-only">{title}</RDialog.Description>
          )}
          {children}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

export function StateMessage({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center px-6 py-16 text-center">
      {icon && <div className="mb-4 text-mute">{icon}</div>}
      <h1 className="m-0 text-xl font-semibold tracking-tight text-ink">{title}</h1>
      {children && <div className="mt-2 text-[14.5px] leading-relaxed text-mute">{children}</div>}
      {action && <div className="mt-6 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}
