// React.lazy for code-split screens, without the "broken until reload" trap:
// React.lazy remembers a failed import for good, so one dropped chunk download
// (a busy server, a flaky network) breaks that screen until the page reloads.
// A failed load is retried once; if it fails again, the next render loads afresh.

import { createElement, lazy, type ComponentProps, type ComponentType } from "react";

// The same bound React.lazy uses.
export function lazyChunk<T extends ComponentType<any>>(load: () => Promise<{ default: T }>): T {
  const make = () =>
    lazy<T>(() =>
      load()
        .catch(() => new Promise<void>((resolve) => setTimeout(resolve, 600)).then(load))
        .catch((error: unknown) => {
          current = make(); // the next render tries again instead of rethrowing this failure
          throw error;
        }),
    );
  let current = make();
  const Chunk = (props: ComponentProps<T>) => createElement(current, props);
  return Chunk as unknown as T;
}
