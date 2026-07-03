import {
  Accordion,
  AccordionButton,
  AccordionIcon,
  AccordionItem,
  AccordionPanel,
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  ButtonProps,
  chakra,
  Checkbox,
  Collapse,
  FormControl,
  FormErrorMessage,
  FormLabel,
  HStack,
  IconButton,
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalHeader,
  ModalOverlay,
  Switch,
  Text,
  Tooltip,
  useToast,
  Wrap,
  WrapItem,
  VStack,
} from "@chakra-ui/react";
import {
  CheckIcon,
  ClipboardDocumentIcon,
  EyeIcon,
  EyeSlashIcon,
  PlusIcon as HeroIconPlusIcon,
  SquaresPlusIcon,
} from "@heroicons/react/24/outline";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  FetchNodesQueryKey,
  getNodeDefaultValues,
  NodeSchema,
  NodeProvisionResponseType,
  NodeProvisionType,
  NodeType,
  useNodes,
  useNodesQuery,
} from "contexts/NodesContext";
import { FC, ReactNode, useEffect, useMemo, useState } from "react";
import { Controller, useForm, UseFormReturn } from "react-hook-form";
import CopyToClipboard from "react-copy-to-clipboard";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import {
  UseMutateFunction,
  useMutation,
  useQuery,
  useQueryClient,
} from "react-query";
import "slick-carousel/slick/slick-theme.css";
import "slick-carousel/slick/slick.css";
import { Status } from "types/User";
import {
  generateErrorMessage,
  generateSuccessMessage,
} from "utils/toastHandler";
import { fetchInbounds, useDashboard } from "../contexts/DashboardContext";
import { DeleteNodeModal } from "./DeleteNodeModal";
import { DeleteIcon } from "./DeleteUserModal";
import { ReloadIcon } from "./Filters";
import { Icon } from "./Icon";
import { NodeModalStatusBadge } from "./NodeModalStatusBadge";

import { fetch } from "service/http";
import { Input } from "./Input";

const CustomInput = chakra(Input, {
  baseStyle: {
    bg: "white",
    _dark: {
      bg: "gray.700",
    },
  },
});

const ModalIcon = chakra(SquaresPlusIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});

const PlusIcon = chakra(HeroIconPlusIcon, {
  baseStyle: {
    w: 5,
    h: 5,
    strokeWidth: 2,
  },
});

const CopyIcon = chakra(ClipboardDocumentIcon, {
  baseStyle: {
    w: 4,
    h: 4,
  },
});

const CopiedIcon = chakra(CheckIcon, {
  baseStyle: {
    w: 4,
    h: 4,
  },
});

const requiredNumberSchema = z.preprocess(
  (value) => (value === "" ? undefined : value),
  z.coerce.number()
);
const portSchema = requiredNumberSchema.pipe(z.number().int().min(1).max(65535));
const numberInputValue = (value: unknown) => (value == null ? "" : String(value));
const parseNumberInput = (value: string | number) =>
  value === "" ? "" : Number(value);

const ProvisionNodeFormSchema = z
  .object({
    name: z.string().min(1),
    address: z.string().min(1),
    port: portSchema,
    api_port: portSchema,
    usage_coefficient: requiredNumberSchema.pipe(z.number().gt(0)),
    hy2: z.boolean(),
    hy2_port: z.unknown(),
    vless_reality: z.boolean(),
    vless_reality_port: z.unknown(),
    shadowsocks: z.boolean(),
    shadowsocks_port: z.unknown(),
  })
  .superRefine((value, ctx) => {
    if (!value.hy2 && !value.vless_reality && !value.shadowsocks) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Select at least one protocol",
        path: ["hy2"],
      });
    }

    [
      [value.hy2, value.hy2_port, "hy2_port"],
      [value.vless_reality, value.vless_reality_port, "vless_reality_port"],
      [value.shadowsocks, value.shadowsocks_port, "shadowsocks_port"],
    ].forEach(([enabled, port, path]) => {
      if (!enabled) return;
      const result = portSchema.safeParse(port);
      if (!result.success) {
        ctx.addIssue({
          ...result.error.issues[0],
          path: [path as string],
        });
      }
    });
  });

const coreColor = (core?: string | null) => {
  if (core === "sing-box") return "purple";
  if (core === "xray") return "blue";
  return "gray";
};

const coreLabel = (core?: string | null) => {
  if (core === "sing-box") return "sing-box";
  if (core === "xray") return "Xray";
  return "unknown";
};

const apiLabel = (available?: boolean | null) => {
  if (available === true) return "available";
  if (available === false) return "unavailable";
  return "unknown";
};

const formatPorts = (ports?: Array<number | string>) => {
  if (!ports || !ports.length) return "-";
  return ports.join(", ");
};

const formatSocketList = (ports?: Array<Record<string, unknown>>) => {
  if (!ports || !ports.length) return "-";
  return ports
    .map((port) => {
      const transport = String(port.transport || port.network || "tcp");
      return `${transport}/${String(port.port || "-")}`;
    })
    .join(", ");
};

const formatConfiguredPorts = (ports?: Array<Record<string, unknown>>) => {
  if (!ports || !ports.length) return "-";
  return ports
    .map((port) => {
      const tag = port.tag ? `${String(port.tag)} ` : "";
      const transport = String(port.transport || port.network || "tcp");
      return `${tag}${transport}/${String(port.port || "-")}`;
    })
    .join(", ");
};

const formatBytes = (bytes?: unknown) => {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "-";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
};

const formatTimestamp = (timestamp?: number | null) => {
  if (!timestamp) return "-";
  return new Date(timestamp * 1000).toLocaleString();
};

const coreInstallLabel = (runtime: NonNullable<NodeType["runtime_status"]>) => {
  const xray = runtime.installed_cores?.xray;
  const singBox = runtime.installed_cores?.["sing-box"];
  const label = (name: string, core: any) => {
    if (!core) return `${name}: unknown`;
    if (!core.installed) return `${name}: missing`;
    return `${name}: ${core.version || "installed"}`;
  };
  return `${label("Xray", xray)} | ${label("sing-box", singBox)}`;
};

const NodeRuntimeSummary: FC<{ node: NodeType }> = ({ node }) => {
  const runtime = node.runtime_status;
  if (!runtime) return null;

  return (
    <Box
      w="full"
      border="1px solid"
      borderColor="gray.200"
      _dark={{ borderColor: "gray.600" }}
      borderRadius="4px"
      p={3}
      mb={3}
    >
      <VStack align="stretch" spacing={2}>
        <HStack justify="space-between" align="start">
          <HStack spacing={2} flexWrap="wrap">
            <Badge colorScheme={coreColor(runtime.actual_core)}>
              current: {coreLabel(runtime.actual_core)}
            </Badge>
            <Badge colorScheme={coreColor(runtime.expected_core)} variant="outline">
              expected: {coreLabel(runtime.expected_core)}
            </Badge>
            <Badge colorScheme={runtime.xray_api_available ? "green" : "gray"}>
              Xray API: {apiLabel(runtime.xray_api_available)}
            </Badge>
          </HStack>
          {runtime.restart_required && (
            <Badge colorScheme="orange">restart required</Badge>
          )}
        </HStack>
        <Text fontSize="xs" color="gray.500">
          {runtime.core_reason}
        </Text>
        <VStack align="stretch" spacing={1} fontSize="xs" color="gray.500">
          <HStack justify="space-between" spacing={3}>
            <Text>node {runtime.node_version || "-"}</Text>
            <Text>last restart {formatTimestamp(runtime.last_core_restart_at)}</Text>
          </HStack>
          <Text>{coreInstallLabel(runtime)}</Text>
          <HStack justify="space-between" spacing={3} align="start">
            <Text>
              memory agent {formatBytes(runtime.memory?.agent_rss_bytes)} / core{" "}
              {formatBytes(runtime.memory?.core_rss_bytes)}
            </Text>
          </HStack>
          <Text>configured {formatConfiguredPorts(runtime.configured_inbound_ports)}</Text>
          <Text>local sockets {formatSocketList(runtime.local_listening_ports)}</Text>
        </VStack>
        {runtime.active_inbounds_details.length > 0 && (
          <VStack align="stretch" spacing={1} pt={1}>
            {runtime.active_inbounds_details.map((inbound) => (
              <HStack
                key={inbound.tag}
                justify="space-between"
                spacing={2}
                fontSize="xs"
                borderTop="1px solid"
                borderColor="gray.100"
                _dark={{ borderColor: "gray.700" }}
                pt={1}
              >
                <HStack spacing={1} minW={0}>
                  <Text fontWeight="medium" noOfLines={1}>
                    {inbound.tag}
                  </Text>
                  <Badge fontSize="0.6rem" colorScheme="blue">
                    {inbound.protocol}
                  </Badge>
                </HStack>
                <HStack spacing={3} flexShrink={0}>
                  <Text color="gray.500">port {formatPorts(inbound.public_ports)}</Text>
                  <Text color="gray.500">users {inbound.users_count}</Text>
                </HStack>
              </HStack>
            ))}
          </VStack>
        )}
      </VStack>
    </Box>
  );
};

type AccordionInboundType = {
  toggleAccordion: () => void;
  node: NodeType;
};

const NodeAccordion: FC<AccordionInboundType> = ({ toggleAccordion, node }) => {
  const { updateNode, reconnectNode, setDeletingNode } = useNodes();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const toast = useToast();
  const form = useForm<NodeType>({
    defaultValues: node,
    resolver: zodResolver(NodeSchema),
  });
  const handleDeleteNode = setDeletingNode.bind(null, node);

  const { isLoading, mutate } = useMutation(updateNode, {
    onSuccess: () => {
      generateSuccessMessage("Node updated successfully", toast);
      queryClient.invalidateQueries(FetchNodesQueryKey);
    },
    onError: (e) => {
      generateErrorMessage(e, toast, form);
    },
  });

  const { isLoading: isReconnecting, mutate: reconnect } = useMutation(
    reconnectNode.bind(null, node),
    {
      onSuccess: () => {
        queryClient.invalidateQueries(FetchNodesQueryKey);
      },
    }
  );

  const nodeStatus: Status = isReconnecting
    ? "connecting"
    : node.status
    ? node.status
    : "error";

  return (
    <AccordionItem
      border="1px solid"
      _dark={{ borderColor: "gray.600" }}
      _light={{ borderColor: "gray.200" }}
      borderRadius="4px"
      p={1}
      w="full"
    >
      <AccordionButton px={2} borderRadius="3px" onClick={toggleAccordion}>
        <HStack w="full" justifyContent="space-between" pr={2}>
          <Text
            as="span"
            fontWeight="medium"
            fontSize="sm"
            flex="1"
            textAlign="left"
            color="gray.700"
            _dark={{ color: "gray.300" }}
          >
            {node.name}
          </Text>
          <HStack>
            {node.runtime_status?.actual_core && (
              <Badge
                colorScheme={coreColor(node.runtime_status.actual_core)}
                rounded="full"
                display="inline-flex"
                px={3}
                py={1}
              >
                <Text fontSize="0.7rem" fontWeight="medium">
                  {coreLabel(node.runtime_status.actual_core)}
                </Text>
              </Badge>
            )}
            {node.xray_version && (
              <Badge
                colorScheme="blue"
                rounded="full"
                display="inline-flex"
                px={3}
                py={1}
              >
                <Text
                  textTransform="capitalize"
                  fontSize="0.7rem"
                  fontWeight="medium"
                >
                  {node.runtime_status?.actual_core
                    ? node.xray_version
                    : `Xray ${node.xray_version}`}
                </Text>
              </Badge>
            )}
            {node.status && <NodeModalStatusBadge status={nodeStatus} compact />}
          </HStack>
        </HStack>
        <AccordionIcon />
      </AccordionButton>
      <AccordionPanel px={2} pb={2}>
        <VStack pb={3} alignItems="flex-start">
          {nodeStatus === "error" && (
            <Alert status="error" size="xs">
              <Box>
                <HStack w="full">
                  <AlertIcon w={4} />
                  <Text marginInlineEnd={0}>{node.message}</Text>
                </HStack>
                <HStack justifyContent="flex-end" w="full">
                  <Button
                    size="sm"
                    aria-label="reconnect node"
                    leftIcon={<ReloadIcon />}
                    onClick={() => reconnect()}
                    disabled={isReconnecting}
                  >
                    {isReconnecting
                      ? t("nodes.reconnecting")
                      : t("nodes.reconnect")}
                  </Button>
                </HStack>
              </Box>
            </Alert>
          )}
        </VStack>
        <NodeRuntimeSummary node={node} />
        <NodeForm
          form={form}
          mutate={mutate}
          isLoading={isLoading}
          submitBtnText={t("nodes.editNode")}
          btnLeftAdornment={
            <Tooltip label={t("delete")} placement="top">
              <IconButton
                colorScheme="red"
                variant="ghost"
                size="sm"
                aria-label="delete node"
                onClick={handleDeleteNode}
              >
                <DeleteIcon />
              </IconButton>
            </Tooltip>
          }
        />
      </AccordionPanel>
    </AccordionItem>
  );
};

type AddNodeFormType = {
  toggleAccordion: () => void;
  resetAccordions: () => void;
};

type ProvisionNodeFormValues = z.infer<typeof ProvisionNodeFormSchema>;

const AddNodeForm: FC<AddNodeFormType> = ({
  toggleAccordion,
  resetAccordions,
}) => {
  const toast = useToast();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addNode, provisionNode } = useNodes();
  const [showManualForm, setShowManualForm] = useState(false);
  const [provisionResult, setProvisionResult] =
    useState<NodeProvisionResponseType | null>(null);
  const [installCommandCopied, setInstallCommandCopied] = useState(false);
  const provisionForm = useForm<ProvisionNodeFormValues>({
    resolver: zodResolver(ProvisionNodeFormSchema),
    defaultValues: {
      name: "",
      address: "",
      port: 62050,
      api_port: 62051,
      usage_coefficient: 1,
      hy2: true,
      hy2_port: 8443,
      vless_reality: false,
      vless_reality_port: 443,
      shadowsocks: false,
      shadowsocks_port: 8388,
    },
  });
  const manualForm = useForm<NodeType>({
    resolver: zodResolver(NodeSchema),
    defaultValues: {
      ...getNodeDefaultValues(),
      add_as_new_host: false,
    },
  });
  const { isLoading: isProvisioning, mutate: provisionMutate } = useMutation(
    provisionNode,
    {
      onSuccess: (result) => {
        setProvisionResult(result);
        setInstallCommandCopied(false);
        generateSuccessMessage(
          t("nodes.addNodeSuccess", { name: provisionForm.getValues("name") }),
          toast
        );
        queryClient.invalidateQueries(FetchNodesQueryKey);
      },
      onError: (e) => {
        setProvisionResult(null);
        generateErrorMessage(e, toast, provisionForm);
      },
    }
  );
  const { isLoading: isManualLoading, mutate: manualMutate } = useMutation(addNode, {
    onSuccess: () => {
      generateSuccessMessage(
        t("nodes.addNodeSuccess", { name: manualForm.getValues("name") }),
        toast
      );
      queryClient.invalidateQueries(FetchNodesQueryKey);
      manualForm.reset();
      resetAccordions();
    },
    onError: (e) => {
      generateErrorMessage(e, toast, manualForm);
    },
  });
  useEffect(() => {
    if (installCommandCopied) {
      const timeout = window.setTimeout(() => {
        setInstallCommandCopied(false);
      }, 1500);
      return () => window.clearTimeout(timeout);
    }
  }, [installCommandCopied]);
  const submitProvision = (values: ProvisionNodeFormValues) => {
    setProvisionResult(null);
    setInstallCommandCopied(false);
    const inbounds: NodeProvisionType["inbounds"] = [];
    if (values.hy2) {
      inbounds.push({ protocol: "hy2", port: Number(values.hy2_port) });
    }
    if (values.vless_reality) {
      inbounds.push({
        protocol: "vless-reality",
        port: Number(values.vless_reality_port),
      });
    }
    if (values.shadowsocks) {
      inbounds.push({
        protocol: "shadowsocks",
        port: Number(values.shadowsocks_port),
      });
    }
    if (!inbounds.length) {
      toast({
        status: "error",
        description: t("nodes.provisionSelectProtocol"),
      });
      return;
    }

    provisionMutate({
      name: values.name,
      address: values.address,
      port: Number(values.port),
      api_port: Number(values.api_port),
      usage_coefficient: Number(values.usage_coefficient),
      inbounds,
    });
  };
  const expectedCore =
    provisionForm.watch("hy2") ? "sing-box" : "Xray";
  const hy2Enabled = provisionForm.watch("hy2");
  const vlessRealityEnabled = provisionForm.watch("vless_reality");
  const shadowsocksEnabled = provisionForm.watch("shadowsocks");
  return (
    <AccordionItem
      border="1px solid"
      _dark={{ borderColor: "gray.600" }}
      _light={{ borderColor: "gray.200" }}
      borderRadius="4px"
      p={1}
      w="full"
    >
      <AccordionButton px={2} borderRadius="3px" onClick={toggleAccordion}>
        <Text
          as="span"
          fontWeight="medium"
          fontSize="sm"
          flex="1"
          textAlign="left"
          color="gray.700"
          _dark={{ color: "gray.300" }}
          display="flex"
          gap={1}
        >
          <PlusIcon display={"inline-block"} />{" "}
          <span>{t("nodes.addNewMarzbanNode")}</span>
        </Text>
      </AccordionButton>
      <AccordionPanel px={2} py={4}>
        <VStack align="stretch" spacing={3}>
          <form onSubmit={provisionForm.handleSubmit(submitProvision)}>
            <VStack align="stretch" spacing={3}>
              <HStack alignItems="flex-start" w="100%">
                <Box w="100%">
                  <CustomInput
                    label={t("nodes.nodeName")}
                    size="sm"
                    placeholder="rn1c1g"
                    error={provisionForm.formState.errors.name?.message}
                    {...provisionForm.register("name", { required: true })}
                  />
                </Box>
                <Box w="100%">
                  <CustomInput
                    label={t("nodes.nodeAddress")}
                    size="sm"
                    placeholder="203.0.113.10"
                    error={provisionForm.formState.errors.address?.message}
                    {...provisionForm.register("address", { required: true })}
                  />
                </Box>
              </HStack>
              <HStack alignItems="flex-start" w="100%">
                <Box>
                  <Controller
                    name="port"
                    control={provisionForm.control}
                    render={({ field }) => (
                      <CustomInput
                        label={t("nodes.nodePort")}
                        size="sm"
                        type="number"
                        placeholder="62050"
                        value={numberInputValue(field.value)}
                        onChange={(value) => field.onChange(parseNumberInput(value))}
                        error={provisionForm.formState.errors.port?.message}
                      />
                    )}
                  />
                </Box>
                <Box>
                  <Controller
                    name="api_port"
                    control={provisionForm.control}
                    render={({ field }) => (
                      <CustomInput
                        label={t("nodes.nodeAPIPort")}
                        size="sm"
                        type="number"
                        placeholder="62051"
                        value={numberInputValue(field.value)}
                        onChange={(value) => field.onChange(parseNumberInput(value))}
                        error={provisionForm.formState.errors.api_port?.message}
                      />
                    )}
                  />
                </Box>
                <Box>
                  <Controller
                    name="usage_coefficient"
                    control={provisionForm.control}
                    render={({ field }) => (
                      <CustomInput
                        label={t("nodes.usageCoefficient")}
                        size="sm"
                        type="number"
                        placeholder="1"
                        value={numberInputValue(field.value)}
                        onChange={(value) => field.onChange(parseNumberInput(value))}
                        error={
                          provisionForm.formState.errors.usage_coefficient?.message
                        }
                      />
                    )}
                  />
                </Box>
              </HStack>
              <FormControl py={1} isInvalid={!!provisionForm.formState.errors.hy2}>
                <FormLabel m={0}>{t("nodes.provisionProtocols")}</FormLabel>
                <VStack align="stretch" spacing={2} pt={2}>
                  <HStack alignItems="center">
                    <Checkbox {...provisionForm.register("hy2")}>HY2</Checkbox>
                    <Controller
                      name="hy2_port"
                      control={provisionForm.control}
                      render={({ field }) => (
                        <CustomInput
                          label={t("nodes.publicPort")}
                          size="sm"
                          type="number"
                          placeholder="8443"
                          value={numberInputValue(field.value)}
                          onChange={(value) =>
                            field.onChange(parseNumberInput(value))
                          }
                          disabled={!hy2Enabled}
                          error={
                            hy2Enabled
                              ? provisionForm.formState.errors.hy2_port?.message
                              : undefined
                          }
                        />
                      )}
                    />
                  </HStack>
                  <HStack alignItems="center">
                    <Checkbox {...provisionForm.register("vless_reality")}>
                      VLESS REALITY
                    </Checkbox>
                    <Controller
                      name="vless_reality_port"
                      control={provisionForm.control}
                      render={({ field }) => (
                        <CustomInput
                          label={t("nodes.publicPort")}
                          size="sm"
                          type="number"
                          placeholder="443"
                          value={numberInputValue(field.value)}
                          onChange={(value) =>
                            field.onChange(parseNumberInput(value))
                          }
                          disabled={!vlessRealityEnabled}
                          error={
                            vlessRealityEnabled
                              ? provisionForm.formState.errors.vless_reality_port?.message
                              : undefined
                          }
                        />
                      )}
                    />
                  </HStack>
                  <HStack alignItems="center">
                    <Checkbox {...provisionForm.register("shadowsocks")}>
                      Shadowsocks
                    </Checkbox>
                    <Controller
                      name="shadowsocks_port"
                      control={provisionForm.control}
                      render={({ field }) => (
                        <CustomInput
                          label={t("nodes.publicPort")}
                          size="sm"
                          type="number"
                          placeholder="8388"
                          value={numberInputValue(field.value)}
                          onChange={(value) =>
                            field.onChange(parseNumberInput(value))
                          }
                          disabled={!shadowsocksEnabled}
                          error={
                            shadowsocksEnabled
                              ? provisionForm.formState.errors.shadowsocks_port?.message
                              : undefined
                          }
                        />
                      )}
                    />
                  </HStack>
                </VStack>
                {provisionForm.formState.errors.hy2 && (
                  <FormErrorMessage>
                    {t("nodes.provisionSelectProtocol")}
                  </FormErrorMessage>
                )}
              </FormControl>
              <HStack>
                <Badge colorScheme={expectedCore === "sing-box" ? "purple" : "blue"}>
                  {t("nodes.expectedCore")}: {expectedCore}
                </Badge>
              </HStack>
              <Button
                type="submit"
                colorScheme="primary"
                size="sm"
                isLoading={isProvisioning}
              >
                {t("nodes.provisionNode")}
              </Button>
            </VStack>
          </form>
          {provisionResult && (
            <Alert status="success" alignItems="start">
              <AlertIcon />
              <AlertDescription w="full" overflow="hidden">
                <HStack justifyContent="space-between" alignItems="center" mb={2}>
                  <Box>
                    <Text fontSize="sm" fontWeight="medium">
                      {t("nodes.installCommand")}
                    </Text>
                    <Text fontSize="xs" color="gray.600" _dark={{ color: "gray.300" }}>
                      {t("nodes.installCommandHint")}
                    </Text>
                  </Box>
                  <CopyToClipboard
                    text={provisionResult.install_command}
                    onCopy={() => setInstallCommandCopied(true)}
                  >
                    <Button
                      size="xs"
                      variant="outline"
                      leftIcon={
                        installCommandCopied ? <CopiedIcon /> : <CopyIcon />
                      }
                    >
                      {installCommandCopied
                        ? t("usersTable.copied")
                        : t("nodes.copyInstallCommand")}
                    </Button>
                  </CopyToClipboard>
                </HStack>
                <HStack mb={2} spacing={2} flexWrap="wrap">
                  <Badge colorScheme={coreColor(provisionResult.core_kind)}>
                    {coreLabel(provisionResult.core_kind)}
                  </Badge>
                  {provisionResult.active_inbounds.map((tag) => (
                    <Badge key={tag} variant="subtle">
                      {tag}
                    </Badge>
                  ))}
                </HStack>
                <Text
                  fontSize="xs"
                  fontFamily="mono"
                  whiteSpace="pre-wrap"
                  wordBreak="break-all"
                  p={2}
                  bg="blackAlpha.100"
                  _dark={{ bg: "whiteAlpha.100" }}
                  borderRadius="4px"
                >
                  {provisionResult.install_command}
                </Text>
              </AlertDescription>
            </Alert>
          )}
          <Button
            size="xs"
            variant="ghost"
            onClick={() => setShowManualForm(!showManualForm)}
          >
            {t("nodes.advancedManualNode")}
          </Button>
          <Collapse in={showManualForm} animateOpacity>
            <NodeForm
              form={manualForm}
              mutate={manualMutate}
              isLoading={isManualLoading}
              submitBtnText={t("nodes.addNode")}
              btnProps={{ variant: "outline" }}
              addAsHost
            />
          </Collapse>
        </VStack>
      </AccordionPanel>
    </AccordionItem>
  );
};

type NodeFormType = FC<{
  form: UseFormReturn<NodeType>;
  mutate: UseMutateFunction<unknown, unknown, any>;
  isLoading: boolean;
  submitBtnText: string;
  btnProps?: Partial<ButtonProps>;
  btnLeftAdornment?: ReactNode;
  addAsHost?: boolean;
}>;

const NodeForm: NodeFormType = ({
  form,
  mutate,
  isLoading,
  submitBtnText,
  btnProps = {},
  btnLeftAdornment,
  addAsHost = false,
}) => {
  const { t } = useTranslation();
  const { inbounds } = useDashboard();
  const [showCertificate, setShowCertificate] = useState(false);
  const inboundOptions = useMemo(
    () =>
      Array.from(inbounds.entries())
        .flatMap(([protocol, inboundList]) =>
          inboundList.map((inbound) => ({
            ...inbound,
            protocol,
          }))
        )
        .sort((a, b) => a.tag.localeCompare(b.tag)),
    [inbounds]
  );
  const { data: nodeSettings, isLoading: nodeSettingsLoading } = useQuery({
    queryKey: "node-settings",
    queryFn: () =>
      fetch<{
        min_node_version: string;
        certificate: string;
      }>("/node/settings"),
  });
  function selectText(node: HTMLElement) {
    // @ts-ignore
    if (document.body.createTextRange) {
      // @ts-ignore
      const range = document.body.createTextRange();
      range.moveToElementText(node);
      range.select();
    } else if (window.getSelection) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(node);
      selection!.removeAllRanges();
      selection!.addRange(range);
    } else {
      console.warn("Could not select text in node: Unsupported browser.");
    }
  }

  return (
    <form
      onSubmit={form.handleSubmit((v) => {
        const { runtime_status, ...body } = v;
        mutate({
          ...body,
          inbounds_mode: body.active_inbounds?.length ? "panel" : "legacy",
        });
      })}
    >
      <VStack>
        {nodeSettings && nodeSettings.certificate && (
          <Alert status="info" alignItems="start">
            <AlertDescription
              display="flex"
              flexDirection="column"
              overflow="hidden"
            >
              <span>{t("nodes.connection-hint")}</span>
              <HStack justify="end" py={2}>
                <Button
                  as="a"
                  colorScheme="primary"
                  size="xs"
                  download="ssl_client_cert.pem"
                  href={URL.createObjectURL(
                    new Blob([nodeSettings.certificate], { type: "text/plain" })
                  )}
                >
                  {t("nodes.download-certificate")}
                </Button>
                <Tooltip
                  placement="top"
                  label={t(
                    !showCertificate
                      ? "nodes.show-certificate"
                      : "nodes.show-certificate"
                  )}
                >
                  <IconButton
                    aria-label={t(
                      !showCertificate
                        ? "nodes.show-certificate"
                        : "nodes.show-certificate"
                    )}
                    onClick={setShowCertificate.bind(null, !showCertificate)}
                    colorScheme="whiteAlpha"
                    color="primary"
                    size="xs"
                  >
                    {!showCertificate ? (
                      <EyeIcon width="15px" />
                    ) : (
                      <EyeSlashIcon width="15px" />
                    )}
                  </IconButton>
                </Tooltip>
              </HStack>
              <Collapse in={showCertificate} animateOpacity>
                <Text
                  bg="rgba(255,255,255,.5)"
                  _dark={{
                    bg: "rgba(255,255,255,.2)",
                  }}
                  rounded="md"
                  p="2"
                  lineHeight="1.2"
                  fontSize="10px"
                  fontFamily="Courier"
                  whiteSpace="pre"
                  overflow="auto"
                  onClick={(e) => {
                    selectText(e.target as HTMLElement);
                  }}
                >
                  {nodeSettings.certificate}
                </Text>
              </Collapse>
            </AlertDescription>
          </Alert>
        )}

        <HStack w="full">
          <FormControl>
            <CustomInput
              label={t("nodes.nodeName")}
              size="sm"
              placeholder="Marzban-S2"
              {...form.register("name")}
              error={form.formState?.errors?.name?.message}
            />
          </FormControl>
          <HStack px={1}>
            <Controller
              name="status"
              control={form.control}
              render={({ field }) => {
                return (
                  <Tooltip
                    key={field.value}
                    placement="top"
                    label={
                      `${t("usersTable.status")}: ` +
                      (field.value !== "disabled" ? t("active") : t("disabled"))
                    }
                    textTransform="capitalize"
                  >
                    <Box mt="6">
                      <Switch
                        colorScheme="primary"
                        isChecked={field.value !== "disabled"}
                        onChange={(e) => {
                          if (e.target.checked) {
                            field.onChange("connecting");
                          } else {
                            field.onChange("disabled");
                          }
                        }}
                      />
                    </Box>
                  </Tooltip>
                );
              }}
            />
          </HStack>
        </HStack>
        <HStack alignItems="flex-start" w="100%">
          <Box w="100%">
            <CustomInput
              label={t("nodes.nodeAddress")}
              size="sm"
              placeholder="51.20.12.13"
              {...form.register("address")}
              error={form.formState?.errors?.address?.message}
            />
          </Box>
        </HStack>
        <HStack alignItems="flex-start" w="100%">
        <Box>
            <CustomInput
              label={t("nodes.nodePort")}
              size="sm"
              placeholder="62050"
              {...form.register("port")}
              error={form.formState?.errors?.port?.message}
            />
          </Box>
          <Box>
            <CustomInput
              label={t("nodes.nodeAPIPort")}
              size="sm"
              placeholder="62051"
              {...form.register("api_port")}
              error={form.formState?.errors?.api_port?.message}
            />
          </Box>
          <Box>
            <CustomInput
              label={t("nodes.usageCoefficient")}
              size="sm"
              placeholder="1"
              {...form.register("usage_coefficient")}
              error={form.formState?.errors?.usage_coefficient?.message}
            />
          </Box>
        </HStack>
        <FormControl py={1}>
          <FormLabel m={0}>{t("nodes.activeInbounds")}</FormLabel>
          <Text fontSize="xs" color="gray.500" mb={2}>
            {t("nodes.activeInboundsHint")}
          </Text>
          <Controller
            name="active_inbounds"
            control={form.control}
            render={({ field }) => {
              const selected = field.value || [];
              return (
                <Wrap spacing={2}>
                  {inboundOptions.map((inbound) => {
                    const isChecked = selected.includes(inbound.tag);
                    return (
                      <WrapItem key={inbound.tag}>
                        <Checkbox
                          size="sm"
                          isChecked={isChecked}
                          onChange={(e) => {
                            if (e.target.checked) {
                              field.onChange([...selected, inbound.tag]);
                            } else {
                              field.onChange(
                                selected.filter((tag) => tag !== inbound.tag)
                              );
                            }
                          }}
                        >
                          <HStack spacing={1}>
                            <Text fontSize="xs">{inbound.tag}</Text>
                            <Badge fontSize="0.6rem" colorScheme="blue">
                              {inbound.protocol}
                            </Badge>
                          </HStack>
                        </Checkbox>
                      </WrapItem>
                    );
                  })}
                </Wrap>
              );
            }}
          />
          {inboundOptions.length === 0 && (
            <Text fontSize="xs" color="gray.500">
              {t("nodes.noInbounds")}
            </Text>
          )}
        </FormControl>
        {addAsHost && (
          <FormControl py={1}>
            <Checkbox {...form.register("add_as_new_host")}>
              <FormLabel m={0}>{t("nodes.addHostForEveryInbound")}</FormLabel>
            </Checkbox>
          </FormControl>
        )}
        <HStack w="full">
          {btnLeftAdornment}
          <Button
            flexGrow={1}
            type="submit"
            colorScheme="primary"
            size="sm"
            px={5}
            w="full"
            isLoading={isLoading}
            {...btnProps}
          >
            {submitBtnText}
          </Button>
        </HStack>
      </VStack>
    </form>
  );
};

export const NodesDialog: FC = () => {
  const { isEditingNodes, onEditingNodes } = useDashboard();
  const { t } = useTranslation();
  const [openAccordions, setOpenAccordions] = useState<any>({});
  const { data: nodes, isLoading } = useNodesQuery();

  useEffect(() => {
    if (isEditingNodes) {
      fetchInbounds();
    }
  }, [isEditingNodes]);

  const onClose = () => {
    setOpenAccordions({});
    onEditingNodes(false);
  };

  const toggleAccordion = (index: number | string) => {
    if (openAccordions[String(index)]) {
      delete openAccordions[String(index)];
    } else openAccordions[String(index)] = {};

    setOpenAccordions({ ...openAccordions });
  };

  return (
    <>
      <Modal isOpen={isEditingNodes} onClose={onClose}>
        <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
        <ModalContent mx="3" w="fit-content" maxW="4xl">
          <ModalHeader pt={6}>
            <Icon color="primary">
              <ModalIcon color="white" />
            </Icon>
          </ModalHeader>
          <ModalCloseButton mt={3} />
          <ModalBody w={{ base: "calc(100vw - 32px)", md: "620px" }} pb={6} pt={3}>
            <Text mb={3} opacity={0.8} fontSize="sm">
              {t("nodes.title")}
            </Text>
            {isLoading && "loading..."}

            <Accordion
              w="full"
              allowToggle
              index={Object.keys(openAccordions).map((i) => parseInt(i))}
            >
              <VStack w="full">
                {!isLoading &&
                  nodes &&
                  nodes.map((node, index) => {
                    return (
                      <NodeAccordion
                        toggleAccordion={() => toggleAccordion(index)}
                        key={node.name}
                        node={node}
                      />
                    );
                  })}

                <AddNodeForm
                  toggleAccordion={() => toggleAccordion((nodes || []).length)}
                  resetAccordions={() => setOpenAccordions({})}
                />
              </VStack>
            </Accordion>
          </ModalBody>
        </ModalContent>
      </Modal>
      <DeleteNodeModal deleteCallback={() => setOpenAccordions({})} />
    </>
  );
};
