// One row style for every table in Settings (founder review): a row that
// opens something is the whole click target, and its name (.hz-row-name, a
// link or button) turns orange on hover, as on Agents and People (app.css).
import type { MouseEvent } from "react";

/** Spread into a Table: `<Table {...ROW_OPENS} />`. A click on another control
 *  in the row is left alone. */
export const ROW_OPENS = {
  rowClassName: "clickable",
  onRow: () => ({ onClick: openRowName }),
} as const;

/** A click on a row (`.clickable` table row or `.hz-row-link` list row) opens
 *  its name's link, unless it landed on another control. A click in a menu or
 *  dialog the row opened reaches the row through React's tree, not the page's:
 *  it is not a click on the row. */
export function openRowName(e: MouseEvent<HTMLElement>) {
  const target = e.target as HTMLElement;
  if (!e.currentTarget.contains(target)) return;
  if (target.closest("a, button, input, textarea, label, .ant-select, .ant-dropdown-trigger, .ant-table-row-expand-icon")) return;
  e.currentTarget.querySelector<HTMLElement>("a.hz-row-name, button.hz-row-name")?.click();
}
