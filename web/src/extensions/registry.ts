import type { ExtensionStatus } from "./types";
import {
  isRunningFrontendExtension,
  parseLoadedFrontendExtension,
  validateFrontendExtensions,
  type ExtensionActivationError,
  type FrontendExtensionValidationResult,
  type LoadedFrontendExtension,
  type LoadedFrontendExtensionResult,
} from "./validation";

type FrontendExtensionLoader = () => Promise<unknown>;
type FrontendExtensionLoadResult = LoadedFrontendExtensionResult;

const moduleLoaders = import.meta.glob<unknown>(
  // Relative to this file (web/src/extensions/registry.ts): one level up is
  // web/src, so this matches web/src/extensions/<id>/frontend/index.tsx.
  // The previous "../../../extensions/..." resolved to <repo-root>/extensions,
  // which does not exist, so the discovery set was always empty.
  "../extensions/*/frontend/index.tsx",
  { import: "default" },
) as Record<string, FrontendExtensionLoader>;

const loaderErrors: ExtensionActivationError[] = [];
const loadersByFrontendId = new Map<string, FrontendExtensionLoader>();

for (const [modulePath, loader] of Object.entries(moduleLoaders)) {
  const match = modulePath.match(/\/extensions\/([^/]+)\/frontend\/index\.tsx$/);
  if (!match) {
    loaderErrors.push({ extensionId: modulePath, message: `无法识别前端扩展入口路径: ${modulePath}` });
    continue;
  }

  const frontendId = match[1].replace(/_/g, "-");
  if (loadersByFrontendId.has(frontendId)) {
    loaderErrors.push({ extensionId: frontendId, message: `前端扩展入口 id "${frontendId}" 重复` });
    continue;
  }
  loadersByFrontendId.set(frontendId, loader);
}

export async function loadRunningFrontendExtensions(
  statuses: ExtensionStatus[],
  reservedPageIds: readonly string[],
): Promise<FrontendExtensionValidationResult> {
  const runningStatuses = statuses.filter(isRunningFrontendExtension);
  const loadResults = await Promise.all(runningStatuses.map(async (status): Promise<FrontendExtensionLoadResult> => {
    const frontendId = status.frontend.trim();
    const loader = loadersByFrontendId.get(frontendId);
    if (!loader) {
      return {
        error: {
          extensionId: status.id,
          message: `未找到 manifest frontend "${status.frontend}" 对应的前端入口`,
        },
      };
    }

    try {
      const extension = await loader();
      return parseLoadedFrontendExtension(status, extension);
    } catch (error) {
      return {
        error: {
          extensionId: status.id,
          message: `前端模块加载失败: ${error instanceof Error ? error.message : String(error)}`,
        },
      };
    }
  }));

  const entries: LoadedFrontendExtension[] = [];
  const errors = [...loaderErrors];
  for (const result of loadResults) {
    if ("entry" in result) {
      entries.push(result.entry);
    } else {
      errors.push(result.error);
    }
  }

  const validation = validateFrontendExtensions(entries, reservedPageIds);
  return {
    extensions: validation.extensions,
    errors: [...errors, ...validation.errors],
  };
}
