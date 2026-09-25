import type { ReactNode } from "react";
import {
  Alert,
  Avatar,
  Button,
  Result,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Bot, Workflow } from "lucide-react";
import type { Permission } from "../api";
import {
  accountStatus,
  capabilityLabel,
  formatTime,
  initials,
  isService,
  personName,
  relativeTime,
  runStatus,
  workflowState,
  type Catalog,
  type Sentence,
  EVERYONE,
} from "../lib/format";
import { href } from "../hooks/useRoute";

const { Text, Title, Paragraph } = Typography;

export function PageHeader({
  eyebrow,
  title,
  description,
  extra,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <Title level={2} style={{ margin: 0 }}>
          {title}
        </Title>
        {description && (
          <Paragraph type="secondary" style={{ margin: "6px 0 0" }}>
            {description}
          </Paragraph>
        )}
      </div>
      {extra && <div className="page-header-extra">{extra}</div>}
    </div>
  );
}

/** Loading skeleton or a recoverable error, for any `useData` record. */
export function LoadState({
  error,
  retry,
  rows = 4,
}: {
  error?: string;
  retry: () => void;
  rows?: number;
}) {
  if (error)
    return (
      <Alert
        type="error"
        showIcon
        title="Couldn’t load this view"
        description={error}
        action={
          <Button size="small" onClick={retry}>
            Try again
          </Button>
        }
      />
    );
  return (
    <div role="status" aria-label="Loading">
      <Skeleton active paragraph={{ rows }} />
    </div>
  );
}

export function RecoveryScreen({
  title,
  subtitle,
  to = "/agents",
  label = "Back to agents",
}: {
  title: string;
  subtitle: string;
  to?: string;
  label?: string;
}) {
  return (
    <Result
      status="404"
      title={title}
      subTitle={subtitle}
      extra={
        <Button type="primary" href={href(to)}>
          {label}
        </Button>
      }
    />
  );
}

export function AccountTag({ status }: { status: string }) {
  const s = accountStatus(status);
  return (
    <Tooltip title={s.hint || undefined}>
      <Tag color={s.color} style={{ marginInlineEnd: 0 }}>
        {s.label}
      </Tag>
    </Tooltip>
  );
}

export function WorkflowStateTag({ state }: { state: string }) {
  const s = workflowState(state);
  return (
    <Tooltip title={s.hint || undefined}>
      <Tag color={s.color} style={{ marginInlineEnd: 0 }}>
        {s.label}
      </Tag>
    </Tooltip>
  );
}

export function RunStatusTag({ status }: { status: string }) {
  const s = runStatus(status);
  return (
    <Tooltip title={s.hint || undefined}>
      <Tag color={s.color} style={{ marginInlineEnd: 0 }}>
        {s.label}
      </Tag>
    </Tooltip>
  );
}

export function CapabilityTag({
  permission,
  catalog,
  via,
}: {
  permission: string;
  catalog?: Catalog | null;
  /** How the capability is held when it is not a direct grant. */
  via?: "inherited" | "public";
}) {
  const meta: Permission | undefined = catalog?.[permission];
  const label = capabilityLabel(permission, catalog);
  const hint = [
    meta?.description,
    meta?.sensitive ? "Sensitive capability." : "",
    via === "inherited"
      ? "Held through organization administrator rights; change it under People."
      : via === "public"
        ? "Held because this agent is open to everyone signed in."
        : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <Tooltip title={hint || undefined}>
      <Tag
        color={meta?.sensitive ? "orange" : via ? "default" : "blue"}
        variant={via ? "outlined" : "filled"}
        style={{ marginInlineEnd: 0 }}
      >
        {label}
        {via === "inherited" ? " · inherited" : via === "public" ? " · everyone" : ""}
      </Tag>
    </Tooltip>
  );
}

export function PersonAvatar({
  subject,
  display,
  size,
}: {
  subject: string;
  display?: string | null;
  size?: number;
}) {
  if (isService(subject))
    return <Avatar size={size} shape="square" className="hz-avatar service" icon={<Workflow size={16} />} />;
  return (
    <Avatar size={size} className={`hz-avatar${subject === EVERYONE ? " everyone" : ""}`}>
      {initials(subject, display)}
    </Avatar>
  );
}

/** Name on top, identity underneath — the identity is what grants attach to. */
export function PersonCell({
  subject,
  display,
  link,
}: {
  subject: string;
  display?: string | null;
  link?: string;
}) {
  const name = personName(subject, display);
  return (
    <Space align="center">
      <PersonAvatar subject={subject} display={display} />
      <div className="person-cell">
        {link ? <a href={link}>{name}</a> : <Text strong>{name}</Text>}
        {name !== subject && (
          <div>
            <Text type="secondary" className="identity">
              {subject}
            </Text>
          </div>
        )}
      </div>
    </Space>
  );
}

export function AgentAvatar({ size = 36 }: { size?: number }) {
  return (
    <Avatar
      size={size}
      shape="square"
      className="hz-avatar agent"
      icon={<Bot size={Math.round(size * 0.55)} />}
    />
  );
}

export function When({ value }: { value: number | string | null | undefined }) {
  return (
    <Tooltip title={formatTime(value)}>
      <span className="when">{relativeTime(value)}</span>
    </Tooltip>
  );
}

export function SentenceText({ sentence }: { sentence: Sentence }) {
  return (
    <div className="sentence">
      <div>
        {sentence.parts.map((part, i) =>
          part.kind ? (
            <Text key={i} strong={part.kind !== "capability"} code={part.kind === "tool"}>
              {part.text}
            </Text>
          ) : (
            <span key={i}>{part.text}</span>
          ),
        )}
      </div>
      {sentence.detail && (
        <Text type="secondary" className="sentence-detail">
          {sentence.detail}
        </Text>
      )}
    </div>
  );
}
