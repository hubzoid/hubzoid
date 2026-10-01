// Keeps an unexpected rendering error (or a page chunk that no longer exists
// after an update) from blanking the whole app: the page shows a plain message
// with Reload, and moving to another page clears it.
import { Component, type ErrorInfo, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { t } from "../i18n/en";
import { Button, StateMessage } from "./ui";

type Props = { resetKey: string; children: ReactNode };
type State = { error: Error | null };

export class PageErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(previous: Props) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.warn("hubzoid: page error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex flex-1 items-center justify-center">
        <StateMessage
          icon={<RefreshCw size={26} aria-hidden />}
          title={t.errors.crashTitle}
          action={
            <Button variant="primary" onClick={() => location.reload()}>
              {t.errors.reload}
            </Button>
          }
        >
          {t.errors.crash}
        </StateMessage>
      </div>
    );
  }
}
