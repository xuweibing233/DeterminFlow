import type { ComponentType, LazyExoticComponent } from "react";
import type { LucideIcon } from "lucide-react";

import type { AgentDefinitionData } from "@/types";

export interface ExtensionPage {
  id: string;
  label: string;
  icon: LucideIcon;
  activeClass: string;
  /** Optional core tab id; when set, the page's nav tab is inserted directly
   *  after that tab instead of being appended to the end of the tab bar. */
  navAfter?: string;
  component: ComponentType | LazyExoticComponent<ComponentType>;
}

export interface AgentExtensionEditorProps {
  agent: AgentDefinitionData;
  updateAgent: (patch: Partial<AgentDefinitionData>) => void;
}

export interface FrontendExtension {
  id: string;
  pages?: ExtensionPage[];
  agentEditor?: ComponentType<AgentExtensionEditorProps>
    | LazyExoticComponent<ComponentType<AgentExtensionEditorProps>>;
}

export interface ExtensionStatus {
  id: string;
  name: string;
  version: string;
  description: string;
  enabled: boolean;
  status: "disabled" | "loaded" | "running" | "degraded" | "failed";
  error: string;
  dependencies: string[];
  capabilities: string[];
  frontend: string;
  header_status: {
    endpoint: string;
    refresh_endpoint: string;
  } | null;
}
