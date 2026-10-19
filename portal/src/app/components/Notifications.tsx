// The bell at the foot of the sidebar: a dot for anything unread, and a small
// inbox of replies that finished elsewhere. Opening a
// notice marks it read; opening the inbox alone does not.
import { useState } from "react";
import { Bell, MoreHorizontal } from "lucide-react";
import { DropdownMenu as Menu, Popover } from "radix-ui";
import { t } from "../i18n/en";
import { formatRelative } from "../lib/format";
import { markAllRead, markNotice, noticeLabel, useNotices } from "../lib/notices";
import { linkClick, navigate } from "../lib/router";
import { cx } from "./ui";

export function NotificationsButton() {
  const items = useNotices();
  const [open, setOpen] = useState(false);
  const unread = items.filter((i) => i.state === "unread").length;

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="hz-icon-btn hz-bell"
          aria-label={unread ? t.notices.unread(unread) : t.notices.title}
          data-testid="notifications"
        >
          <Bell size={17} aria-hidden />
          {unread > 0 && <span className="hz-bell-dot" aria-hidden />}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="hz-menu hz-inbox" side="top" align="start" sideOffset={8} collisionPadding={8}>
          <div className="hz-inbox-head">
            <span>{t.notices.title}</span>
            {unread > 0 && (
              <button type="button" className="hz-link-btn" onClick={() => void markAllRead()}>
                {t.notices.markAll}
              </button>
            )}
          </div>
          {items.length === 0 ? (
            <p className="m-0 px-4 py-6 text-center text-[12.5px] leading-relaxed text-mute">{t.notices.empty}</p>
          ) : (
            <ul className="m-0 max-h-[400px] list-none overflow-y-auto p-1">
              {items.map((item) => (
                <li key={item.id} className={cx("hz-inbox-row", item.state === "unread" && "is-unread")} data-state={item.state}>
                  <a
                    href={item.link}
                    className="min-w-0 flex-1 no-underline"
                    onClick={(e) => {
                      if (item.state === "unread") void markNotice(item.id, "read").catch(() => {});
                      setOpen(false);
                      linkClick(e, item.link);
                    }}
                  >
                    <span className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{noticeLabel(item.kind)}</span>
                      <span className="flex-none text-[11px] tabular-nums text-mute">{formatRelative(item.created_at)}</span>
                    </span>
                    <span className="block truncate text-[12.5px] text-secondary">{item.title || t.sidebar.untitled}</span>
                  </a>
                  <Menu.Root modal={false}>
                    <Menu.Trigger asChild>
                      <button type="button" className="hz-icon-btn hz-inbox-more" aria-label={t.notices.options}>
                        <MoreHorizontal size={14} aria-hidden />
                      </button>
                    </Menu.Trigger>
                    <Menu.Portal>
                      <Menu.Content className="hz-menu w-[180px]" align="end" sideOffset={4}>
                        {item.state === "unread" && (
                          <Menu.Item className="hz-menu-item" onSelect={() => void markNotice(item.id, "read").catch(() => {})}>
                            {t.notices.markRead}
                          </Menu.Item>
                        )}
                        <Menu.Item className="hz-menu-item" onSelect={() => void markNotice(item.id, "dismissed").catch(() => {})}>
                          {t.notices.dismiss}
                        </Menu.Item>
                      </Menu.Content>
                    </Menu.Portal>
                  </Menu.Root>
                </li>
              ))}
            </ul>
          )}
          <div className="hz-inbox-foot">
            <button
              type="button"
              className="hz-link-btn"
              onClick={() => {
                setOpen(false);
                navigate("/settings?section=notifications");
              }}
            >
              {t.notices.settings}
            </button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
