// True when a screen is shown under a tab row that already names it (a
// Settings group's tabs, an agent's tabs). The screen then leaves out its own
// title and lead line, so a page never says its name twice (HUB-140 F39).
import { createContext, useContext } from "react";

export const UnderTabs = createContext(false);

export function useUnderTabs(): boolean {
  return useContext(UnderTabs);
}
