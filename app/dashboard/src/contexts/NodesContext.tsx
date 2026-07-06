import { useQuery } from "react-query";
import { fetch } from "service/http";
import { z } from "zod";
import { create } from "zustand";
import { FilterUsageType, useDashboard } from "./DashboardContext";

const NodeInboundRuntimeDetailSchema = z.object({
  tag: z.string(),
  protocol: z.string(),
  network: z.string().nullable().optional(),
  tls: z.string().nullable().optional(),
  port: z.number().or(z.string()).nullable().optional(),
  public_port: z.number().or(z.string()).nullable().optional(),
  public_ports: z.array(z.number().or(z.string())).default([]),
  users_count: z.number().default(0),
});

const NodeRuntimeStatusSchema = z.object({
  active_inbounds_details: z.array(NodeInboundRuntimeDetailSchema).default([]),
  expected_core: z.string().nullable().optional(),
  actual_core: z.string().nullable().optional(),
  core_reason: z.string(),
  xray_api_available: z.boolean().nullable().optional(),
  restart_required: z.boolean().default(false),
  node_version: z.string().nullable().optional(),
  installed_cores: z.record(z.string(), z.any()).default({}),
  memory: z.record(z.string(), z.any()).default({}),
  local_listening_ports: z.array(z.record(z.string(), z.any())).default([]),
  configured_inbound_ports: z.array(z.record(z.string(), z.any())).default([]),
  last_core_restart_at: z.number().nullable().optional(),
});

export const NodeSchema = z.object({
  name: z.string().min(1),
  address: z.string().min(1),
  port: z
    .number()
    .min(1)
    .or(z.string().transform((v) => parseFloat(v))),
  api_port: z
    .number()
    .min(1)
    .or(z.string().transform((v) => parseFloat(v))),
  active_inbounds: z.array(z.string()).default([]),
  inbounds_mode: z.enum(["legacy", "panel"]).default("legacy"),
  xray_version: z.string().nullable().optional(),
  id: z.number().nullable().optional(),
  status: z
    .enum(["connected", "connecting", "error", "disabled"])
    .nullable()
    .optional(),
  message: z.string().nullable().optional(),
  runtime_status: NodeRuntimeStatusSchema.nullable().optional(),
  add_as_new_host: z.boolean().optional(),
  usage_coefficient: z.number().or(z.string().transform((v) => parseFloat(v))),
});

export type NodeType = z.infer<typeof NodeSchema>;

export const NodeProvisionProtocolSchema = z.enum([
  "hy2",
  "anytls",
  "vless-reality",
  "shadowsocks",
]);

export const NodeProvisionSchema = z.object({
  name: z.string().min(1),
  address: z.string().min(1),
  port: z.number().or(z.string().transform((v) => parseFloat(v))),
  api_port: z.number().or(z.string().transform((v) => parseFloat(v))),
  usage_coefficient: z.number().or(z.string().transform((v) => parseFloat(v))),
  inbounds: z.array(
    z.object({
      protocol: NodeProvisionProtocolSchema,
      port: z.number().or(z.string().transform((v) => parseFloat(v))),
      reality_server_name: z.string().optional(),
    })
  ),
});

export type NodeProvisionType = z.infer<typeof NodeProvisionSchema>;

export const NodeProvisionResponseSchema = z.object({
  node: NodeSchema,
  active_inbounds: z.array(z.string()),
  core_kind: z.string(),
  install_token: z.string(),
  install_command: z.string(),
});

export type NodeProvisionResponseType = z.infer<
  typeof NodeProvisionResponseSchema
>;

export const getNodeDefaultValues = (): NodeType => ({
  name: "",
  address: "",
  port: 62050,
  api_port: 62051,
  active_inbounds: [],
  inbounds_mode: "legacy",
  xray_version: "",
  usage_coefficient: 1,
});

export const FetchNodesQueryKey = "fetch-nodes-query-key";

export type NodeStore = {
  nodes: NodeType[];
  addNode: (node: NodeType) => Promise<unknown>;
  provisionNode: (
    node: NodeProvisionType
  ) => Promise<NodeProvisionResponseType>;
  fetchNodes: () => Promise<NodeType[]>;
  fetchNodesUsage: (query: FilterUsageType) => Promise<void>;
  updateNode: (node: NodeType) => Promise<unknown>;
  reconnectNode: (node: NodeType) => Promise<unknown>;
  deletingNode?: NodeType | null;
  deleteNode: () => Promise<unknown>;
  setDeletingNode: (node: NodeType | null) => void;
};

export const useNodesQuery = () => {
  const { isEditingNodes } = useDashboard();
  return useQuery({
    queryKey: FetchNodesQueryKey,
    queryFn: useNodes.getState().fetchNodes,
    refetchInterval: isEditingNodes ? 3000 : undefined,
    refetchOnWindowFocus: false,
  });
};

export const useNodes = create<NodeStore>((set, get) => ({
  nodes: [],
  addNode(body) {
    return fetch("/node", { method: "POST", body });
  },
  provisionNode(body) {
    return fetch("/node/provision", { method: "POST", body });
  },
  fetchNodes() {
    return fetch("/nodes");
  },
  fetchNodesUsage(query: FilterUsageType) {
    return fetch("/nodes/usage", { query });
  },
  updateNode(body) {
    return fetch(`/node/${body.id}`, {
      method: "PUT",
      body,
    });
  },
  setDeletingNode(node) {
    set({ deletingNode: node });
  },
  reconnectNode(body) {
    return fetch(`/node/${body.id}/reconnect`, {
      method: "POST",
    });
  },
  deleteNode: () => {
    return fetch(`/node/${get().deletingNode?.id}`, {
      method: "DELETE",
    });
  },
}));
