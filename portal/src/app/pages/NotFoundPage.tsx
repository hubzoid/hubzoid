import { useEffect } from "react";
import { Menu as MenuIcon, SearchX } from "lucide-react";
import { t } from "../i18n/en";
import { useApp } from "../lib/app-context";
import { Button, IconButton, StateMessage } from "../components/ui";

export default function NotFoundPage() {
  const app = useApp();
  useEffect(() => {
    document.title = `${t.notFound.title} · ${app.brandName}`;
  }, [app.brandName]);
  return (
    <>
      <header className="flex h-14 flex-none items-center border-b border-line px-3 lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={20} aria-hidden />
        </IconButton>
      </header>
      <div className="flex flex-1 items-center justify-center">
        <StateMessage
          icon={<SearchX size={28} aria-hidden />}
          title={t.notFound.title}
          action={
            <Button variant="primary" onClick={() => app.newChat()}>
              {t.goHome}
            </Button>
          }
        >
          {t.notFound.body}
        </StateMessage>
      </div>
    </>
  );
}
