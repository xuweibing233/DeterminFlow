import { lazy } from "react";
import { BookMarked } from "lucide-react";

import type { FrontendExtension } from "../../types";

// Full-bleed mount for the novel-reader plugin's static UI. The iframe is the
// page itself (no card chrome), so the shelf reads as a native tab; the plugin
// backend keeps serving the UI and its API routes.
const NovelReaderShelfPage = lazy(() => import("./ShelfPage"));

const extension: FrontendExtension = {
  id: "novel-reader",
  pages: [
    {
      id: "novel-reader",
      label: "书架",
      icon: BookMarked,
      activeClass: "data-[state=active]:bg-emerald-500/20 data-[state=active]:text-emerald-400",
      navAfter: "dashboard",
      component: NovelReaderShelfPage,
    },
  ],
};

export default extension;
