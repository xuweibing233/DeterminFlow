const PLUGIN_UI_URL = "/api/plugins/novel-reader/ui/index.html";

/**
 * Full-height, chrome-less mount for the novel-reader static UI. The plugin
 * backend serves the page same-origin, so the iframe can call
 * /api/novel-reader/* without any credential plumbing.
 */
export default function ShelfPage() {
  return (
    <iframe
      src={PLUGIN_UI_URL}
      title="书架"
      className="h-[calc(100dvh-3.5rem)] w-full border-0 bg-background"
    />
  );
}
