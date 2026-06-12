import { NavLink, useLocation } from "react-router-dom";
import { RefreshCw } from "lucide-react";

/**
 * Shared top-right nav used by the root page (MeetingList),
 * ChannelsAdmin, and ArchiveBrowser. One source of truth so adding /
 * renaming routes touches one file.
 *
 * Refresh defaults to `window.location.reload()` — heavy but works
 * everywhere without per-page wiring. Pass `onRefresh` to override
 * with a page-local re-fetch if you want something snappier.
 */
export function TopNav({ onRefresh }: { onRefresh?: () => void }) {
  const { pathname } = useLocation();

  const link = (path: string, label: string) => (
    <NavLink
      to={path}
      className={() => {
        const active =
          path === "/"
            ? pathname === "/" || pathname.startsWith("/m")
            : pathname.startsWith(path);
        return [
          "rounded-md px-3 py-1.5 text-xs font-medium transition",
          active
            ? "bg-gold-500/20 text-gold-200 ring-1 ring-gold-500/40"
            : "text-ink-200 hover:bg-ink-800 hover:text-ink-50",
        ].join(" ");
      }}
    >
      {label}
    </NavLink>
  );

  return (
    <nav className="ml-auto flex items-center gap-1">
      {link("/", "Meetings")}
      {link("/channels", "Channels")}
      {link("/clients", "Clients")}
      {link("/archive", "Archive")}
      <button
        type="button"
        onClick={() => (onRefresh ? onRefresh() : window.location.reload())}
        title="Refresh"
        className="ml-2 flex items-center gap-1 rounded-md border border-ink-700 bg-ink-900 px-3 py-1.5 text-xs text-ink-200 transition hover:bg-ink-800"
      >
        <RefreshCw size={12} />
        Refresh
      </button>
    </nav>
  );
}
