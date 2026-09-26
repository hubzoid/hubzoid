import type { ReactNode } from "react";
import { Tag, Typography } from "antd";
import { ChevronDown, CircleHelp } from "lucide-react";

const { Text } = Typography;

/**
 * Help that works without hover: a real button that reveals its explanation
 * inline, for keyboard, touch and screen-reader users alike. The text is a
 * separate element so a caller can place it below the row it explains.
 */
export function HelpToggle({
  label,
  open,
  controls,
  onToggle,
}: {
  label: string;
  open: boolean;
  controls: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="capability-info"
      aria-label={`About ${label}`}
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
    >
      <CircleHelp size={16} aria-hidden="true" />
    </button>
  );
}

export function HelpText({ id, open, children }: { id: string; open: boolean; children: ReactNode }) {
  return (
    <div id={id} className="help-text" hidden={!open}>
      {children}
    </div>
  );
}

/**
 * One capability group. Collapsible groups start closed and say what they hold
 * in the header; the rows stay mounted while closed, so nothing selected is
 * lost. Always-open groups (entry, and removable leftovers) have no toggle.
 */
export function CapabilityGroup({
  id,
  title,
  collapsible,
  open,
  onToggle,
  selected,
  unconfigured,
  problems,
  children,
}: {
  id: string;
  title: string;
  collapsible: boolean;
  open: boolean;
  onToggle: () => void;
  selected: number;
  unconfigured: number;
  problems: number;
  children: ReactNode;
}) {
  const titleId = `capability-group-${id}`;
  const panelId = `${titleId}-panel`;
  const shown = !collapsible || open;
  return (
    <div className="capability-group" role="group" aria-labelledby={titleId}>
      <h3 className="capability-group-heading">
        {collapsible ? (
          <button
            type="button"
            className="capability-group-toggle"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={onToggle}
          >
            {/* Spaces between parts keep the accessible name readable:
                "Restricted tools · 2 selected". Flex layout ignores them. */}
            <span className="capability-group-title" id={titleId}>{title}</span>
            {selected > 0 && <>{" "}<Text type="secondary" className="capability-group-meta">· {selected} selected</Text></>}
            {unconfigured > 0 && (
              <>{" "}<Text type="warning" className="capability-group-meta">· {unconfigured} not configured</Text></>
            )}
            {problems > 0 && <>{" "}<Text type="danger" className="capability-group-meta">· needs attention</Text></>}
            <ChevronDown size={16} aria-hidden="true" className="capability-group-chevron" />
          </button>
        ) : (
          <span className="capability-group-title" id={titleId}>{title}</span>
        )}
      </h3>
      <div id={panelId} className="capability-group-panel" hidden={!shown}>
        {children}
      </div>
    </div>
  );
}

/** A `workflow:<name>` identity from before workflows ran as user accounts. */
export function LegacyServiceTag() {
  return (
    <Tag color="default" className="legacy-service-tag" style={{ marginInlineEnd: 0 }}>
      Legacy service identity
    </Tag>
  );
}
