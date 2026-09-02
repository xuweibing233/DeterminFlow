"""
AgentNode — 工作流 Agent 节点插件。

为每个 Agent 节点创建独立的子会话，注入工作流上下文，
等待 Agent 通过 complete_node_task 工具通知完成。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from src.config import WORKFLOW_NODE_TIMEOUT_SECONDS
from src.core.utils import message_content_text
from src.workflow.json_output import (
    build_json_retry_prompt,
    detect_output_format,
    get_json_error_context,
    get_json_policy,
    get_json_retry_count,
    validate_and_format_json,
)
from src.workflow.output_validation import validate_agent_output

from .base import BaseNodePlugin, NodeContext, NodeResult
from ..definition import resolve_placeholders, _now_iso
from ..failure_policy import RECOVERY_REISSUE_TRIGGER
from ..variable_resolution import resolve_workspace_file_path

logger = logging.getLogger(__name__)

_EMPTY_OUTPUT_RETRY_PROMPT = (
    "你上一条回复没有最终正文。请现在只输出最终正文，不要调用任何工具。"
)

MAX_REJECTION_COUNT = 3


class AgentNode(BaseNodePlugin):
    node_type = "agent"
    label = "Agent 节点"

    @property
    def params_schema(self) -> list[dict]:
        return [
            {
                "key": "agent_type",
                "label": "Agent 类型",
                "type": "select",
                "required": True,
                "default": "default",
                "description": "选择执行此节点的 Agent 类型",
                # options 由前端从 /api/workflows/agent-types/list 动态加载
            },
            {
                "key": "label",
                "label": "节点名称",
                "type": "text",
                "required": False,
                "default": "",
                "description": "节点在画布上的显示名称",
            },
            {
                "key": "system_prompt_template",
                "label": "System Prompt 补充",
                "type": "textarea",
                "required": False,
                "default": "",
                "description": "注入到 system prompt 的额外指令，支持 {{key}} 占位符",
            },
            {
                "key": "first_message",
                "label": "首条任务消息",
                "type": "textarea",
                "required": True,
                "default": "",
                "description": "发送给 Agent 的第一条消息（任务描述），支持 {{key}} 占位符",
            },
            {
                "key": "auto_flow",
                "label": "自动流转",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "开启后 Agent 输出完成即视为成功，无需调用 complete_node_task",
            },
            {
                "key": "enable_complete_node_task",
                "label": "注入 complete_node_task 工具",
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "关闭后 Agent 无法显式调用完成/失败工具",
            },
            {
                "key": "enable_reject_upstream",
                "label": "注入 reject_upstream 工具",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "开启后 Agent 可以拒绝上游节点的产出，触发工作流回滚重试",
            },
            {
                "key": "max_reject_count",
                "label": "最大拒绝次数",
                "type": "text",
                "required": False,
                "default": "3",
                "description": "允许上游节点被拒绝的最大次数（enable_reject_upstream 为 true 时生效）",
            },
            {
                "key": "output_variable",
                "label": "最后输出加载为变量",
                "type": "text",
                "required": False,
                "default": "",
                "description": "开启后，Agent 最后一轮回复文本将写入此变量，供后续节点通过 {{变量名}} 引用",
            },
            {
                "key": "require_non_empty_output",
                "label": "要求 LLM 输出非空",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "开启后，最后一条 LLM 输出为空时节点失败，并进入节点自动重试策略",
            },
            {
                "key": "retry_empty_output_in_session",
                "label": "空输出时在原会话追问一次",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "仅在要求 LLM 输出非空时生效；追问禁用工具，仍为空才进入节点重试",
            },
            {
                "key": "json_output_field",
                "label": "JSON 字段路径",
                "type": "text",
                "required": False,
                "default": "",
                "description": "可选，例如 body 或 data.chapters.0.body；需与最小字数同时配置",
            },
            {
                "key": "json_output_field_min_chars",
                "label": "JSON 字段最小字数",
                "type": "text",
                "required": False,
                "default": "0",
                "description": "指定字符串字段的字数必须严格大于此值；0 表示关闭",
            },
            {
                "key": "save_output_to_file",
                "label": "保存最后输出到文件",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "开启后，Agent 最后一轮回复文本将保存到指定文件。路径支持 workspace 内绝对路径、相对路径和 {{key}} 占位符",
            },
            {
                "key": "output_file_path",
                "label": "文件路径",
                "type": "text",
                "required": False,
                "default": "",
                "description": "保存路径。推荐相对路径；绝对路径仅允许位于当前 workspace 内。支持 {{key}} 占位符",
            },
            {
                "key": "output_format",
                "label": "输出格式",
                "type": "select",
                "required": False,
                "default": "",
                "description": "可选 text/json/markdown。留空时 .json 文件自动视为 json",
                "options": [
                    {"label": "自动", "value": ""},
                    {"label": "文本", "value": "text"},
                    {"label": "JSON", "value": "json"},
                    {"label": "Markdown", "value": "markdown"},
                ],
            },
            {
                "key": "json_repair_policy",
                "label": "JSON 修复策略",
                "type": "select",
                "required": False,
                "default": "",
                "description": "JSON 输出校验失败时的处理策略。留空时 .json 文件默认 safe_repair_then_retry",
                "options": [
                    {"label": "自动", "value": ""},
                    {"label": "不处理", "value": "none"},
                    {"label": "只校验", "value": "validate_only"},
                    {"label": "安全修复", "value": "safe_repair"},
                    {"label": "安全修复后重试", "value": "safe_repair_then_retry"},
                    {"label": "仅重试", "value": "retry_only"},
                ],
            },
            {
                "key": "json_retry_count",
                "label": "JSON 重试次数",
                "type": "text",
                "required": False,
                "default": "1",
                "description": "JSON 校验失败后追加修复指令的最大次数",
            },
            {
                "key": "model_override",
                "label": "模型覆盖",
                "type": "select",
                "required": False,
                "default": "",
                "description": "覆盖此节点使用的模型（格式 provider_id:model_name），留空则使用 Agent 类型的默认模型。支持 {{key}} 占位符",
                # options 由前端从 /api/models/list 动态加载
            },
        ]

    async def execute(self, ctx: NodeContext) -> NodeResult:
        """执行 Agent 节点：创建子会话 → 等待完成 → 可选审批。"""
        sm = ctx.session_manager
        node_def = ctx.node_def
        pv = ctx.parameter_values
        workflow_id = ctx.workflow_id

        # 变量替换（含嵌套引用 + 文件变量读取）
        variables = ctx.definition.variables
        shared_ws = str(ctx.shared_ws) if ctx.shared_ws else ""
        resolved_agent_type = await resolve_placeholders(node_def.agent_type, pv, variables, shared_ws)
        resolved_system_prompt = await resolve_placeholders(node_def.system_prompt_template, pv, variables, shared_ws)
        resolved_first_message = await resolve_placeholders(node_def.first_message, pv, variables, shared_ws)

        # 读取节点配置
        auto_flow = getattr(node_def, "auto_flow", False)
        enable_complete_node_task = getattr(node_def, "enable_complete_node_task", True)
        output_var_key = getattr(node_def, "output_variable", "")
        enable_reject_upstream = getattr(node_def, "enable_reject_upstream", False)
        max_reject_count = getattr(node_def, "max_reject_count", 3)
        save_output_to_file = getattr(node_def, "save_output_to_file", False)
        output_file_path_raw = getattr(node_def, "output_file_path", "")
        require_non_empty_output = getattr(
            node_def, "require_non_empty_output", False,
        )
        retry_empty_output_in_session = getattr(
            node_def, "retry_empty_output_in_session", False,
        )
        json_output_field = getattr(node_def, "json_output_field", "")
        json_output_field_min_chars = getattr(
            node_def, "json_output_field_min_chars", 0,
        )
        model_override_raw = getattr(node_def, "model_override", "")
        model_override = (await resolve_placeholders(model_override_raw, pv, variables, shared_ws)) if model_override_raw else None
        from ..task_overrides import MODEL_PARAMS_OVERRIDE_KEY

        model_params_override = (node_def.node_params or {}).get(
            MODEL_PARAMS_OVERRIDE_KEY
        )
        if not isinstance(model_params_override, dict):
            model_params_override = {}

        # 新 Task 在快照中冻结 Agent prompt/model 运行身份。每次创建新的
        # sub session 前重新核验，防止长任务中途配置漂移。
        from ..runtime_guards import RUNTIME_GUARD_KEY, RUNTIME_GUARD_SCHEMA

        runtime_guard = (node_def.node_params or {}).get(RUNTIME_GUARD_KEY)
        if runtime_guard is not None:
            if not isinstance(runtime_guard, dict) or runtime_guard.get(
                "schema_version"
            ) != RUNTIME_GUARD_SCHEMA:
                return NodeResult(
                    status="failed",
                    error="Workflow Agent 运行身份守卫无效",
                )
            expected_agent_type = runtime_guard.get("agent_type")
            expected_model_override = runtime_guard.get("model_override", "")
            expected_sha256 = runtime_guard.get(
                "effective_agent_definition_sha256"
            )
            expected_model_params_sha256 = runtime_guard.get(
                "model_params_override_sha256"
            )
            from ..runtime import effective_agent_definition_sha256

            model_params_guard_mismatch = (
                expected_model_params_sha256
                != effective_agent_definition_sha256(model_params_override)
                if expected_model_params_sha256 is not None
                else bool(model_params_override)
            )
            if (
                expected_agent_type != resolved_agent_type
                or expected_model_override != (model_override or "")
                or model_params_guard_mismatch
                or not expected_sha256
            ):
                return NodeResult(
                    status="failed",
                    error=(
                        "Workflow Agent 节点解析结果与 Task 冻结身份不一致，"
                        "拒绝启动新 session"
                    ),
                )
            verifier = getattr(sm, "verify_effective_agent_definition", None)
            if not callable(verifier):
                return NodeResult(
                    status="failed",
                    error="SessionManager 未提供 Agent 运行身份核验器",
                )
            try:
                verifier(
                    resolved_agent_type,
                    expected_sha256=expected_sha256,
                    model_override=model_override,
                    model_params_override=model_params_override,
                )
            except Exception as exc:
                logger.error(
                    "Agent 节点 %s 运行身份核验失败: %s",
                    node_def.id,
                    exc,
                )
                return NodeResult(status="failed", error=str(exc))

        # 读取并解析自定义变量块（template_values）
        raw_template_vars = (node_def.node_params or {}).get("template_values", {})
        # 兼容两种格式：前端保存的 JSON 字符串 或 直接传入的 dict
        if isinstance(raw_template_vars, str):
            try:
                raw_template_vars = json.loads(raw_template_vars) or {}
            except (json.JSONDecodeError, TypeError):
                raw_template_vars = {}
        if not isinstance(raw_template_vars, dict):
            raw_template_vars = {}
        resolved_template_vars: dict[str, str] = {}
        if raw_template_vars:
            for key, value in raw_template_vars.items():
                if value:
                    # 解析变量块值中的 workflow 变量引用（如 {{file_content}}）
                    resolved_template_vars[key] = await resolve_placeholders(value, pv, variables, shared_ws)
                else:
                    resolved_template_vars[key] = ""

        # 完成信号
        completion_event = asyncio.Event()
        completion_result = {"summary": "", "status": "success", "error": ""}
        _complete_called = False  # 标记 complete_node_task 是否已显式调用
        approval_requested = False

        async def handle_approval(summary: str, status: str, error: str) -> None:
            nonlocal completion_result
            approval_error = await self._handle_approval(
                ctx, summary, status, error, completion_event, sm,
            )
            if approval_error:
                completion_result = {
                    "summary": summary,
                    "status": "failed",
                    "error": approval_error,
                }

        def request_approval(summary: str, status: str, error: str) -> None:
            nonlocal approval_requested, completion_result
            if approval_requested:
                return
            approval_requested = True
            completion_result = {
                "summary": summary,
                "status": status,
                "error": error,
            }
            asyncio.create_task(handle_approval(summary, status, error))

        def on_node_complete(session_id: str, summary: str, status: str, error: str):
            nonlocal _complete_called, completion_result
            _complete_called = True
            if ctx.needs_approval:
                request_approval(summary, status, error)
            else:
                completion_result = {"summary": summary, "status": status, "error": error}
                # 不在此处 set completion_event，因为 LLM 在调用
                # complete_node_task 后可能还会生成一条最终回复。
                # 由 on_auto_complete 在 graph 完全结束后统一设置。
                self._push_node_status(workflow_id, node_def.id, session_id, summary, status, error)

        def on_auto_complete(session_id: str, summary: str, status: str, error: str):
            """graph 完全结束后触发（_auto_first_message 的 finally 块中调用）。"""
            nonlocal _complete_called, completion_result
            logger.debug(
                "[AGENT-NODE] on_auto_complete 触发: node=%s, session=%s, status=%s",
                node_def.id, session_id, status,
            )
            if ctx.needs_approval:
                if not _complete_called:
                    _complete_called = True
                    request_approval(summary, status, error)
                return
            if _complete_called:
                # on_node_complete 已存储结果，只需触发事件
                completion_event.set()
                return
            _complete_called = True
            completion_result = {"summary": summary, "status": status, "error": error}
            completion_event.set()
            self._push_node_status(workflow_id, node_def.id, session_id, summary, status, error)

        reissuing_approval = (
            ctx.needs_approval
            and ctx.node_state.next_attempt_trigger == RECOVERY_REISSUE_TRIGGER
            and bool(ctx.node_state.session_id)
            and bool(ctx.node_state.summary)
        )
        if reissuing_approval:
            session_id = ctx.node_state.session_id
            completion_result = {
                "summary": ctx.node_state.summary,
                "status": "success",
                "error": "",
            }
            await handle_approval(
                completion_result["summary"],
                completion_result["status"],
                completion_result["error"],
            )
        else:
            # 创建子会话
            try:
                result = await sm.create_sub_session(
                    task_description=resolved_first_message,
                    custom_prompt=resolved_system_prompt,
                    agent_type=resolved_agent_type,
                    workspace_path=str(ctx.shared_ws) if ctx.shared_ws else None,
                    is_workflow_node=True,
                    on_node_complete=on_node_complete,
                    parent_id=ctx.parent_id,
                    workflow_id=workflow_id,
                    task_id=ctx.task_id,
                    upstream_summary=ctx.upstream_summary,
                    node_id=node_def.id,
                    auto_flow=auto_flow,
                    enable_complete_node_task=enable_complete_node_task,
                    on_auto_complete=on_auto_complete,
                    template_vars=resolved_template_vars or None,
                    on_reject_upstream=(
                        ctx.on_reject_upstream if enable_reject_upstream else None
                    ),
                    model_override=model_override,
                    model_params_override=model_params_override,
                )
            except Exception as e:
                logger.exception(f"Agent 节点 {node_def.id} 创建子会话失败")
                return NodeResult(status="failed", error=str(e))

            if not result.get("success"):
                return NodeResult(
                    status="failed",
                    error=result.get("message", "创建子会话失败"),
                )

            session_id = result["session_id"]
            ctx.node_state.session_id = session_id
            if ctx.checkpoint:
                await ctx.checkpoint()
            logger.debug(
                "[AGENT-NODE] 子会话已创建: node=%s, session=%s, task_id=%s",
                node_def.id, session_id, ctx.task_id,
            )

        # 等待完成（带超时）
        try:
            timeout = WORKFLOW_NODE_TIMEOUT_SECONDS
            logger.debug(
                "[AGENT-NODE] 等待 completion_event: node=%s, session=%s, task=%s, timeout=%ds",
                node_def.id, session_id, ctx.task_id, timeout,
            )
            wait_start = time.time()
            await asyncio.wait_for(completion_event.wait(), timeout=timeout)
            wait_elapsed = time.time() - wait_start
            logger.debug(
                "[AGENT-NODE] completion_event 被唤醒: node=%s, session=%s, task=%s, 等待时长=%.1fs",
                node_def.id, session_id, ctx.task_id, wait_elapsed,
            )
        except asyncio.TimeoutError:
            node_token_usage = self._get_session_token_usage(sm, session_id)
            return NodeResult(
                status="failed", error=f"节点执行超时 ({timeout}秒)",
                session_id=session_id,
                token_usage=node_token_usage,
            )

        # 从 session 读取累计 token 消耗
        node_token_usage = self._get_session_token_usage(sm, session_id)

        validation_enabled = bool(
            require_non_empty_output
            or json_output_field
            or json_output_field_min_chars
        )
        validated_output = ""
        if completion_result["status"] == "success" and validation_enabled:
            validated_output = self._get_latest_ai_message(sm, session_id)
            if (
                require_non_empty_output
                and retry_empty_output_in_session
                and not validated_output.strip()
            ):
                retry_error = await self._retry_empty_output_in_session(
                    sm, session_id,
                )
                node_token_usage = self._get_session_token_usage(sm, session_id)
                if retry_error:
                    return NodeResult(
                        summary=completion_result["summary"],
                        status="failed",
                        error=retry_error,
                        session_id=session_id,
                        outputs={},
                        token_usage=node_token_usage,
                    )
                validated_output = self._get_latest_ai_message(sm, session_id)
            validation = validate_agent_output(
                validated_output,
                require_non_empty=require_non_empty_output,
                json_field=json_output_field,
                json_field_min_chars=json_output_field_min_chars,
            )
            if not validation.success:
                return NodeResult(
                    summary=completion_result["summary"],
                    status="failed",
                    error=f"LLM 输出校验失败: {validation.error}",
                    session_id=session_id,
                    outputs={},
                    token_usage=node_token_usage,
                )

        # 构建 NodeResult，提取输出变量
        outputs: dict[str, str] = {}
        if output_var_key and completion_result["status"] == "success":
            last_msg = (
                validated_output if validation_enabled
                else self._get_latest_ai_message(sm, session_id)
            )
            if last_msg:
                outputs[output_var_key] = last_msg

        # ---- 保存最后输出到文件 ----
        if save_output_to_file and output_file_path_raw and completion_result["status"] == "success":
            last_msg_for_file = outputs.get(output_var_key) if output_var_key else \
                (
                    validated_output if validation_enabled
                    else self._get_latest_ai_message(sm, session_id)
                )
            if last_msg_for_file:
                try:
                    # 解析路径中的 {{key}} 占位符
                    resolved_path = await resolve_placeholders(
                        output_file_path_raw, pv, variables, shared_ws,
                    )
                    file_path = resolve_workspace_file_path(
                        shared_ws,
                        resolved_path,
                    )

                    # 确保父目录存在
                    file_path.parent.mkdir(parents=True, exist_ok=True)

                    output_text = last_msg_for_file
                    json_meta: dict[str, str] = {}
                    output_format = detect_output_format(str(file_path), node_def.node_params)
                    if output_format == "json":
                        json_result = await self._prepare_json_output(
                            sm=sm,
                            session_id=session_id,
                            raw_output=last_msg_for_file,
                            node_params=node_def.node_params,
                            output_file_path=str(file_path),
                        )
                        node_token_usage = self._get_session_token_usage(
                            sm, session_id,
                        )
                        if not json_result["success"]:
                            return NodeResult(
                                summary=completion_result["summary"],
                                status="failed",
                                error=json_result.get("error", "JSON 输出校验失败"),
                                session_id=session_id,
                                outputs=outputs,
                                token_usage=node_token_usage,
                            )
                        output_text = json_result["text"]
                        json_meta = json_result.get("meta", {})
                        if output_var_key:
                            outputs[output_var_key] = output_text
                        outputs.update(json_meta)

                    # 文件已存在则警告
                    if file_path.exists():
                        logger.warning(f"覆盖已存在文件: {file_path}")

                    # 写入文件
                    file_path.write_text(output_text, encoding="utf-8")
                    outputs["_output_file"] = str(file_path)
                    logger.info(f"LLM输出已保存到文件: {file_path}")
                    if json_meta:
                        completion_result["summary"] = self._append_json_summary(
                            completion_result["summary"], json_meta,
                        )
                except ValueError as e:
                    # 占位符解析失败
                    return NodeResult(
                        summary=completion_result["summary"],
                        status="failed",
                        error=f"保存输出文件路径占位符解析失败: {e}",
                        session_id=session_id,
                        outputs=outputs,
                    )
                except Exception as e:
                    # IO 错误
                    logger.exception(f"保存LLM输出到文件失败: {e}")
                    return NodeResult(
                        summary=completion_result["summary"],
                        status="failed",
                        error=f"保存输出文件失败: {e}",
                        session_id=session_id,
                        outputs=outputs,
                    )
            else:
                error = (
                    f"节点 {node_def.id} 开启了 save_output_to_file，"
                    "但没有最终 AI 输出可保存"
                )
                logger.error(error)
                return NodeResult(
                    summary=completion_result["summary"],
                    status="failed",
                    error=error,
                    session_id=session_id,
                    outputs={},
                    token_usage=node_token_usage,
                )

        return NodeResult(
            summary=completion_result["summary"],
            status="completed" if completion_result["status"] == "success" else "failed",
            error=completion_result.get("error", ""),
            session_id=session_id,
            outputs=outputs,
            token_usage=node_token_usage,
        )

    async def _prepare_json_output(
        self,
        sm,
        session_id: str,
        raw_output: str,
        node_params: dict | None,
        output_file_path: str,
    ) -> dict:
        """校验并准备 JSON 文件输出，必要时复用同一子会话重试。"""
        policy = get_json_policy(output_file_path, node_params)
        retry_count = get_json_retry_count(node_params)
        meta: dict[str, str] = {
            "_json_repair_policy": policy,
            "_json_output_file": output_file_path,
        }

        if policy == "none":
            return {"success": True, "text": raw_output, "meta": meta}

        allow_repair = policy in {"safe_repair", "safe_repair_then_retry"}
        result = validate_and_format_json(raw_output, repair=allow_repair)
        if result.success:
            if result.repairs:
                meta["_json_repair_applied"] = ",".join(result.repairs)
            meta["_json_retry_attempts"] = "0"
            return {"success": True, "text": result.formatted, "meta": meta}

        allow_retry = policy in {"safe_repair_then_retry", "retry_only"} and retry_count > 0
        if not allow_retry:
            return {
                "success": False,
                "error": f"JSON 输出校验失败: {result.error}",
                "meta": meta,
            }

        session = self._resolve_session(sm, session_id)
        if not session or not hasattr(session, "send_message"):
            return {
                "success": False,
                "error": f"JSON 输出校验失败，且无法复用子会话重试: {result.error}",
                "meta": meta,
            }

        current_output = result.repaired_text or raw_output
        last_error = result.error
        setup_error = self._configure_tool_free_retry_graph(session)
        if setup_error:
            return {
                "success": False,
                "error": f"JSON 输出校验失败，且无法安全重试: {setup_error}",
                "meta": meta,
            }
        for attempt in range(1, retry_count + 1):
            prompt = build_json_retry_prompt(
                last_error,
                get_json_error_context(current_output, last_error),
            )
            try:
                await session.send_message(
                    prompt,
                    max_rounds=1,
                    source="workflow_json_retry",
                    source_name="workflow_json_validator",
                )
            except Exception as e:
                logger.exception("JSON 输出修复重试调用失败: session=%s", session_id)
                return {
                    "success": False,
                    "error": f"JSON 输出修复重试调用失败: {e}",
                    "meta": meta,
                }

            retry_output = self._get_latest_ai_message(sm, session_id)
            retry_result = validate_and_format_json(
                retry_output,
                repair=policy == "safe_repair_then_retry",
            )
            if retry_result.success:
                repairs = list(result.repairs or []) + list(retry_result.repairs or [])
                if repairs:
                    meta["_json_repair_applied"] = ",".join(dict.fromkeys(repairs))
                meta["_json_retry_attempts"] = str(attempt)
                return {"success": True, "text": retry_result.formatted, "meta": meta}
            current_output = retry_result.repaired_text or retry_output
            last_error = retry_result.error

        return {
            "success": False,
            "error": f"JSON 输出重试 {retry_count} 次后仍校验失败: {last_error}",
            "meta": meta,
        }

    @staticmethod
    def _append_json_summary(summary: str, meta: dict[str, str]) -> str:
        """在节点摘要中附加 JSON 输出处理结果。"""
        attempts = meta.get("_json_retry_attempts", "0")
        repairs = meta.get("_json_repair_applied", "")
        note = f"JSON 输出已校验并格式化；重试次数: {attempts}"
        if repairs:
            note += f"；安全修复: {repairs}"
        return f"{summary}\n\n{note}" if summary else note

    @staticmethod
    async def _retry_empty_output_in_session(sm, session_id: str) -> str:
        """在原子会话追问一次，并从 Graph 层禁用全部工具。"""
        session = AgentNode._resolve_session(sm, session_id)
        if not session or not hasattr(session, "send_message"):
            return "LLM 最终输出为空，且无法复用原子会话追问"

        try:
            setup_error = AgentNode._configure_tool_free_retry_graph(session)
            if setup_error:
                return setup_error
            await session.send_message(
                _EMPTY_OUTPUT_RETRY_PROMPT,
                max_rounds=1,
                source="workflow_empty_output_retry",
                source_name="workflow_output_validator",
            )
        except Exception as exc:
            logger.exception("LLM 空输出原会话追问失败: session=%s", session_id)
            return f"LLM 空输出原会话追问失败: {exc}"
        return ""

    @staticmethod
    def _configure_tool_free_retry_graph(session) -> str:
        """为输出修正调用重建无工具 Graph，杜绝重复执行副作用。"""
        if not hasattr(session, "setup_graph"):
            return "原子会话无法禁用工具后重试"
        model_id = getattr(session, "model_id", None)
        if not model_id:
            return "原子会话缺少已冻结模型，无法安全重试"
        try:
            from src.core.llm_client import create_llm

            retry_llm = create_llm(
                model_override=model_id,
                streaming=True,
                model_params=getattr(session, "model_params", None) or {},
            )
            session.setup_graph(llm=retry_llm, tools=[])
        except Exception as exc:
            logger.exception("输出修正无工具 Graph 创建失败: session=%s", session.session_id)
            return f"输出修正无工具 Graph 创建失败: {exc}"
        return ""

    @staticmethod
    def _resolve_session(sm, session_id: str):
        """优先读取热会话，必要时使用 SessionManager 的统一查询入口。"""
        session = sm.sessions.get(session_id) if hasattr(sm, "sessions") else None
        if session is not None:
            return session
        getter = getattr(sm, "get_session", None)
        return getter(session_id) if callable(getter) else None

    @staticmethod
    def _strip_inline_reasoning(text: str) -> str:
        """剥离模型内联在正文里的 <think>...</think> 推理块。

        DeepSeek 系把推理放在独立的 reasoning_content 字段，但 MiniMax M 系列
        等模型把思考过程以 <think> 标签内联在 content 开头。工作流输出契约
        （纯 JSON、章节正文等）只应包含净载荷。
        """
        if "<think>" not in text:
            return text
        return re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.DOTALL)

    @classmethod
    def _get_latest_ai_message(cls, sm, session_id: str) -> str:
        """读取最新 assistant 消息；不得用更早的非空消息替代空输出。"""
        session = AgentNode._resolve_session(sm, session_id)
        if not session:
            return ""
        for msg in reversed(session.record):
            if msg.get("type") == "assistant":
                return cls._strip_inline_reasoning(
                    message_content_text(msg.get("content"))
                )
        return ""

    @staticmethod
    def _get_session_token_usage(sm, session_id: str) -> dict | None:
        """从已完成的 session 读取累计 token 使用数据。

        Returns:
            dict: 按 model_id 分组的累计 token 数据，无数据时返回 None
        """
        session = AgentNode._resolve_session(sm, session_id)
        if not session or not hasattr(session, "get_cumulative_token_usage"):
            return None
        cumulative = session.get_cumulative_token_usage()
        return cumulative if cumulative else None

    # ----------------------------------------------------------
    # 审批处理（从 engine._handle_approval_request 迁移）
    # ----------------------------------------------------------

    async def _fail_approval_delivery(
        self,
        ctx: NodeContext,
        summary: str,
        completion_event: asyncio.Event,
        reason: str,
    ) -> str:
        failure_error = f"审批请求发送失败: {reason}"
        ctx.node_state.status = "failed"
        ctx.node_state.error = failure_error
        if ctx.checkpoint:
            await ctx.checkpoint()
        logger.error(
            "审批请求发送失败: node=%s, main=%s, reason=%s",
            ctx.node_def.id,
            ctx.parent_id,
            reason,
        )
        self._push_node_status(
            ctx.workflow_id,
            ctx.node_def.id,
            ctx.node_state.session_id,
            summary,
            "failed",
            failure_error,
        )
        completion_event.set()
        return failure_error

    async def _handle_approval(
        self, ctx: NodeContext, summary: str, status: str, error: str,
        completion_event: asyncio.Event, sm,
    ) -> str | None:
        """向 workflow-main 发送审批请求，等待 approve_node 工具调用。"""
        from src.workflow.prompt_injector import build_workflow_summary_for_approval
        from src.workflow.engine import get_current_engine

        engine = get_current_engine()
        if engine is None:
            return await self._fail_approval_delivery(
                ctx,
                summary,
                completion_event,
                "WorkflowEngine 未初始化",
            )
        approval_key = (ctx.workflow_id, ctx.task_id, ctx.node_def.id)
        approval_event = asyncio.Event()
        engine._pending_approvals[approval_key] = approval_event
        engine._approval_results[approval_key] = {}

        result_: dict = {}
        try:
            ctx.node_state.status = "waiting_approval"
            ctx.node_state.summary = summary if status == "success" else error
            if ctx.checkpoint:
                await ctx.checkpoint()

            # 找到 main session
            main_id = ctx.parent_id
            if not (main_id and main_id in sm.sessions):
                return await self._fail_approval_delivery(
                    ctx,
                    summary,
                    completion_event,
                    f"main session {main_id or '<empty>'} 不存在",
                )
            approval_msg = build_workflow_summary_for_approval(
                ctx.definition,
                ctx.task_id,
                ctx.node_def.id,
                summary,
                ctx.node_state.attempt_count,
            )
            try:
                route_result = await sm.route_message(
                    from_session_id=ctx.node_state.session_id or main_id,
                    to_session_id=main_id, content=approval_msg,
                )
            except Exception as e:
                return await self._fail_approval_delivery(
                    ctx,
                    summary,
                    completion_event,
                    str(e),
                )
            if not isinstance(route_result, dict):
                return await self._fail_approval_delivery(
                    ctx,
                    summary,
                    completion_event,
                    "消息路由未返回有效结果",
                )
            if route_result.get("success") is not True:
                return await self._fail_approval_delivery(
                    ctx,
                    summary,
                    completion_event,
                    str(route_result.get("message") or "消息路由拒绝请求"),
                )
            logger.info(f"审批请求已发送到 main {main_id}: node={ctx.node_def.id}")

            try:
                await asyncio.wait_for(approval_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                logger.warning(f"审批超时: node={ctx.node_def.id}，自动通过")
                engine._approval_results[approval_key] = {
                    "approved": True,
                    "feedback": "审批超时，自动通过",
                }
        finally:
            result_ = engine._approval_results.pop(approval_key, {})
            engine._pending_approvals.pop(approval_key, None)
        approved = result_.get("approved", True)
        feedback = result_.get("feedback", "")

        if approved:
            logger.info(f"节点通过审批: {ctx.node_def.id}")
            completion_event.set()
            self._push_node_status(
                ctx.workflow_id, ctx.node_def.id, ctx.node_state.session_id,
                summary, "success", "",
            )
        else:
            ctx.node_state.rejection_count += 1
            ctx.node_state.rejection_reason = feedback
            if ctx.node_state.rejection_count >= MAX_REJECTION_COUNT:
                logger.warning(f"节点 {ctx.node_def.id} 已被拒绝 {ctx.node_state.rejection_count} 次，强制通过")
                completion_event.set()
                self._push_node_status(
                    ctx.workflow_id, ctx.node_def.id, ctx.node_state.session_id,
                    summary + f"\n[强制通过: 已拒绝 {ctx.node_state.rejection_count} 次]",
                    "success", "",
                )
            else:
                logger.info(f"节点被拒绝: {ctx.node_def.id} (第 {ctx.node_state.rejection_count} 次)")
                self._push_node_status(
                    ctx.workflow_id, ctx.node_def.id, ctx.node_state.session_id,
                    summary, "waiting_approval",
                    f"被拒绝 {ctx.node_state.rejection_count} 次: {feedback}",
                )
                rejection_msg = (
                    f"## 审批拒绝\n\n"
                    f"你的产出被 Main Agent 拒绝了（第 {ctx.node_state.rejection_count}/{MAX_REJECTION_COUNT} 次）。\n"
                    f"拒绝原因: {feedback}\n\n"
                    f"请根据以上反馈重新完成任务，完成后再次调用 complete_node_task。"
                )
                sub_sid = ctx.node_state.session_id
                if sub_sid and sub_sid in sm.sessions:
                    sub_session = sm.sessions[sub_sid]
                    await sub_session.enqueue(
                        content=rejection_msg, priority=1,
                        source=f"approval:{main_id}",
                        event_callback=sm._make_event_callback(sub_sid),
                    )
                    ctx.node_state.status = "running"
                    if ctx.checkpoint:
                        await ctx.checkpoint()

    @staticmethod
    def _push_node_status(workflow_id: str, node_id: str, session_id: str,
                           summary: str, status: str, error: str):
        """推送节点状态事件 — 委托给 nodes/base.py 公共函数。"""
        from src.workflow.nodes.base import push_node_status
        push_node_status(workflow_id, node_id, session_id, summary, status, error)
