"""Pydantic models for YAML configuration validation.

Pure data models — no runtime imports (Agent, MCPClient, etc.).
Validation catches user errors at parse time with clear messages.

Key Features:
    - Discriminated union for orchestration modes (delegate, swarm, graph)
    - Cross-section name collision detection via joint namespaces
    - Reference field descriptors for automated name sanitization
    - Inline and named model/hook/session_manager resolution
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, Field, model_validator


class ModelDef(BaseModel):
    """LLM model configuration."""

    provider: str
    model_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class HookDef(BaseModel):
    """Hook provider reference.

    ``type`` must be a ``module.path:ClassName`` import path or a
    ``./file.py:ClassName`` file-based import path.  The resolver raises
    ``ValueError`` if there is no colon separator.  ``params`` are forwarded
    as constructor kwargs.
    """

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConversationManagerDef(BaseModel):
    """Conversation manager configuration.

    ``type`` must be a ``module.path:ClassName`` import path or a
    ``./file.py:ClassName`` file-based import path.  The resolver raises
    ``ValueError`` if there is no colon separator.  ``params`` are forwarded
    as constructor kwargs.

    Built-in strands classes:

    - ``strands.agent:SlidingWindowConversationManager``
    - ``strands.agent:SummarizingConversationManager``
    - ``strands.agent:NullConversationManager``
    """

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class SessionManagerDef(BaseModel):
    """Session manager configuration.

    Built-in providers: ``"file"``, ``"s3"``, ``"agentcore"``.

    For a custom class, set ``type`` to an import path
    (``"module.path:ClassName"``).  The class must be a subclass of
    ``strands.session.SessionManager``.  When ``type`` is set, ``provider``
    is ignored.

    Session ID resolution order:
      1. Runtime override (e.g., HTTP session header)
      2. ``params.session_id``
      3. Random UUID (fresh session per CLI run)
    """

    provider: str = "file"
    type: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class MCPServerDef(BaseModel):
    """MCP server definition."""

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class MCPClientAuthDef(BaseModel):
    """Outbound auth for an MCP client (streamable-http / SSE only).

    ``type`` selects a built-in strategy from
    :func:`~kaboo_workflows.auth.build_auth`:

    - ``relay`` — forward the inbound caller token (see
      :class:`~kaboo_workflows.auth.RelayTokenAuth`).
    - ``obo`` — AgentCore On-Behalf-Of exchange
      (:class:`~kaboo_workflows.auth.OBOTokenAuth`); requires ``provider``.
      Provider-specific differences stay in ``params``: ``workload_name`` to
      mint the workload access token from the inbound user token, and
      ``custom_parameters`` for anything the identity provider expects on the
      exchange (an Entra ID provider wants
      ``requested_token_use: on_behalf_of``).
    - ``m2m`` — client-credentials machine token
      (:class:`~kaboo_workflows.auth.M2MClientCredentialsAuth`).
    - ``static`` — a fixed token (:class:`~kaboo_workflows.auth.StaticTokenAuth`).

    ``params`` are forwarded as constructor kwargs to the chosen strategy.
    ``header`` accepts a list as well as a single name, which is what a managed
    AgentCore Gateway needs: it authenticates the caller on ``Authorization``
    and then replaces that header with its own outbound credential, so a token
    the target must also see has to be sent twice.

    Note: ``relay`` / ``obo`` derive from the *per-request* caller identity and
    are reliable only when the MCP client is created per request (started inside
    the request context). On a long-lived shared client (``create_agui_app``),
    prefer ``static`` / ``m2m`` (machine identity) unless the deployment is
    process-per-session (e.g. AgentCore Runtime).
    """

    type: Literal["relay", "obo", "m2m", "static"]
    params: dict[str, Any] = Field(default_factory=dict)


class MCPClientDef(BaseModel):
    """MCP client connection definition.

    Exactly one of ``server``, ``url``, or ``command`` must be set.

    ``params`` are forwarded to strands MCPClient (e.g., startup_timeout,
    tool_filters, prefix). ``transport_options`` are forwarded to the
    transport factory (e.g., headers, auth, timeout, http_client). ``auth``
    declaratively attaches an outbound auth strategy (see
    :class:`MCPClientAuthDef`).
    """

    server: str | None = None
    url: str | None = None
    command: list[str] | None = None
    transport: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    transport_options: dict[str, Any] = Field(default_factory=dict)
    tool_labels: dict[str, str] = Field(default_factory=dict)
    auth: MCPClientAuthDef | str | None = None

    @model_validator(mode="after")
    def _normalize_auth(self) -> MCPClientDef:
        """Normalize ``auth: <strategy>`` shorthand to a full MCPClientAuthDef."""
        if isinstance(self.auth, str):
            self.auth = MCPClientAuthDef(type=cast("Any", self.auth))
        return self

    @model_validator(mode="after")
    def _exactly_one_connection_mode(self) -> MCPClientDef:
        """Validate that exactly one of server/url/command is set."""
        modes = [self.server is not None, self.url is not None, self.command is not None]
        count = sum(modes)
        if count == 0:
            raise ValueError(
                "MCPClientDef requires exactly one of 'server', 'url', or 'command'; got none."
            )
        if count > 1:
            raise ValueError(
                "MCPClientDef requires exactly one of 'server', 'url', or 'command'; got multiple."
            )
        if self.auth is not None and self.command is not None:
            raise ValueError(
                "MCPClientDef 'auth' is not supported for stdio (command) transport; "
                "use 'url' or 'server' (HTTP/SSE)."
            )
        return self


class InterruptDef(BaseModel):
    """Interrupt configuration for an agent.

    ``tools`` lists tool names that require user approval before execution.
    An empty list means no tool-gate (use ``ask_user`` only).

    ``ask_user`` controls whether the built-in ``ask_user`` tool is injected,
    allowing the agent to proactively ask the user questions at any time.
    Defaults to ``True`` when interrupt is enabled.

    ``ttl_seconds`` optionally stamps an ``expiresAt`` timestamp onto every
    gate interrupt this agent raises (AG-UI ``Interrupt.expiresAt``), so
    clients can render a countdown and servers can expire unanswered
    approvals. ``None`` (default) emits no expiry.
    """

    tools: list[str] = Field(default_factory=list)
    ask_user: bool = True
    ttl_seconds: int | None = Field(default=None, gt=0)


class StreamDef(BaseModel):
    """Stream group configuration for agent activity visibility.

    Both fields are optional — when omitted, the agent's declared name
    is used as the group and a humanized version as the title.
    """

    group: str | None = None
    title: str | None = None


class HistoryDef(BaseModel):
    """Per-agent conversation history configuration.

    History is client-driven and stateless on the server: an agent's
    transcript travels in the AG-UI ``state`` blob (``state.kaboo_history``),
    keyed per agent, and is seeded/captured around each invocation.

    - ``enabled`` — whether this agent remembers across turns of the same
      conversation. When false the agent runs fresh each turn.
    - ``group`` — optional shared-transcript bucket. Agents that declare the
      same ``group`` share one history key, so they read/write a single
      transcript (e.g. ``researcher`` + ``fact_checker`` sharing context).
      When omitted, the agent's stable dot-path is used as its key.
    """

    enabled: bool = True
    group: str | None = None


class AttachmentsDef(BaseModel):
    """Global reference/attachment behavior.

    References (file attachments and custom entities cited via ``@`` in the
    frontend) are propagated to in-scope agents as a lightweight text manifest,
    with an optional shared tool to fetch/resolve them on demand.

    - ``default`` — baseline policy for agents that do not set their own
      ``attachments:``. ``"reference"`` injects the manifest; ``"none"``
      excludes agents by default.
    - ``tool`` — expose the built-in ``list_references`` / ``fetch_attachment``
      tools so in-scope agents can resolve a reference to text/bytes/URL.
    - ``base_url`` — origin for the host's own attachment content routes.
      Relative reference URLs resolve against it, and ``authorization`` applies
      only to URLs under it (credentials are never sent to other origins;
      presigned/public URLs keep the default unauthenticated fetch).
    - ``authorization`` — where to read the bearer token for own-origin
      fetches: ``forwarded_props:<key>`` (run-scoped token from the AG-UI
      forwardedProps side channel) or ``env:<VAR>`` (static token).
    - ``content_url_template`` — URL template (``{id}`` placeholder, relative
      to ``base_url`` or absolute) for attachment-kind object references that
      arrive without a URL. Setting it upgrades those references to fetchable
      attachment transport on every turn, not only the first.
    """

    default: Literal["reference", "none"] = "reference"
    tool: bool = True
    base_url: str | None = None
    authorization: str | None = None
    content_url_template: str | None = None

    @model_validator(mode="after")
    def _validate_fetch_config(self) -> AttachmentsDef:
        if self.authorization is not None and not self.authorization.startswith(
            ("forwarded_props:", "env:")
        ):
            raise ValueError(
                "attachments.authorization must be 'forwarded_props:<key>' or 'env:<VAR>', "
                f"got {self.authorization!r}"
            )
        if self.content_url_template is not None and "{id}" not in self.content_url_template:
            raise ValueError(
                "attachments.content_url_template must contain the '{id}' placeholder, "
                f"got {self.content_url_template!r}"
            )
        return self


class TelemetryOTLPDef(BaseModel):
    """OTLP trace export target.

    - ``endpoint`` — OTLP/HTTP base URL (e.g. a collector or Langfuse's
      ``/api/public/otel``). ``/v1/traces`` is appended when missing. Falls
      back to ``KABOO_OTLP_ENDPOINT`` then ``OTEL_EXPORTER_OTLP_ENDPOINT``.
    - ``headers`` — request headers, either a mapping or a ``k=v,k2=v2``
      string (the ``OTEL_EXPORTER_OTLP_HEADERS`` wire format). Falls back to
      ``KABOO_OTLP_HEADERS`` then ``OTEL_EXPORTER_OTLP_HEADERS``.
    """

    endpoint: str | None = None
    headers: dict[str, str] | str | None = None


class TelemetryDef(BaseModel):
    """Opt-in OpenTelemetry tracing (off by default, zero-overhead when off).

    When enabled, the process initializes strands' OpenTelemetry integration
    (agent / model / tool spans with GenAI semantic conventions) and exports
    them via OTLP to any backend (Langfuse, Phoenix, a bare collector, …).
    Every span is additionally stamped with kaboo's conversation context
    (``session.id`` = thread id, ``user.id`` from the authenticated caller,
    ``kaboo.run.id`` / ``kaboo.turn.id``) plus any static ``trace_attributes``.

    Telemetry is process-wide: it initializes once at boot from the base
    config. Per-run session configs cannot toggle it. The
    ``KABOO_TELEMETRY_ENABLED`` env var overrides ``enabled`` in either
    direction (kill switch / opt-in without a config edit).

    ``sample_ratio`` applies parent-based trace-id ratio sampling (1.0 =
    every trace) so large pipelines can trace a fraction of traffic.
    """

    enabled: bool = False
    service_name: str = "kaboo-workflows"
    otlp: TelemetryOTLPDef = Field(default_factory=TelemetryOTLPDef)
    console: bool = False
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    trace_attributes: dict[str, Any] = Field(default_factory=dict)


class RuntimeDef(BaseModel):
    """Runtime behavior toggles.

    - ``allow_invocation_overrides`` — **deprecated, removed in 0.15.0.** When
      ``True``, a run's ``forwardedProps.agent_config`` (``system_prompt`` /
      ``model_id``) is applied to the executing per-thread agent before each
      invocation. Submit the run's whole config instead
      (``create_agui_app(session_config_key=...)``): it expresses the prompt and
      model along with the structure this flag could not reach.
    - ``persist_session_state`` — carry agent session state (pending interrupts)
      on the AG-UI state channel, so a paused approval survives a restart
      without configuring a session store. On by default, because the AG-UI
      client is normally a host server and the channel is server-to-server. Set
      ``False`` when the endpoint is exposed directly to browsers: a client that
      can edit gate state could approve its own interrupts.
    """

    allow_invocation_overrides: bool = False
    persist_session_state: bool = True


class AgentAttachmentsDef(BaseModel):
    """Per-agent reference policy (normalized form).

    - ``enabled`` — whether this agent is in scope for references at all. When
      ``False`` the agent receives no manifest (``attachments: none``).
    - ``inline`` — whether the agent additionally receives resolved media as
      strands ``ContentBlock``s so a vision/doc-capable model literally sees the
      file (heavier; opt-in per agent).
    """

    enabled: bool = True
    inline: bool = False


class AgentDef(BaseModel):
    """Top-level agent definition.

    All agents are defined flat under the ``agents:`` section.
    Multi-agent orchestration is configured separately in the ``orchestrations:`` section.

    ``tools`` accepts spec strings:

    - ``"module.path:function_name"`` — single function from module
    - ``"module.path"`` — all ``@tool`` functions in module
    - ``"./path/to/file.py:function_name"`` — single function from file
    - ``"./path/to/file.py"`` — all ``@tool`` functions in file
    - ``"./path/to/dir/"`` — all ``@tool`` functions in directory

    ``hooks`` accepts import-path strings (``"module.path:ClassName"`` or
    ``"./file.py:ClassName"``) or inline :class:`HookDef` objects with
    explicit type + optional params.
    """

    type: str | None = None
    """Custom agent factory import path.

    Format: ``module.path:ClassName`` or ``./file.py:ClassName``.
    When set, the factory is called instead of ``strands.Agent()`` directly.
    The ``agent_kwargs`` dict is spread as ``**kwargs`` to this factory.
    """
    agent_kwargs: dict[str, Any] = Field(default_factory=dict)
    """Additional keyword arguments passed to strands.Agent() or custom factory.

    Valid Agent parameters: messages, callback_handler,
    record_direct_tool_call, trace_attributes, state, plugins,
    structured_output_prompt, structured_output_model, tool_executor,
    retry_strategy, concurrent_invocation_mode, load_tools_from_directory.

    Warning: Use at your own risk — no schema-level validation is performed.
    Agent.__init__ has 24 explicit parameters and no **kwargs; any invalid
    key will raise TypeError at construction time.
    """
    model: str | ModelDef | None = None
    system_prompt: str | None = None
    description: str | None = None
    tools: list[str] = Field(default_factory=list)
    hooks: list[HookDef | str] = Field(default_factory=list)
    mcp: list[str] = Field(default_factory=list)
    tool_labels: dict[str, str] = Field(default_factory=dict)
    conversation_manager: ConversationManagerDef | None = None
    session_manager: SessionManagerDef | None = None
    stream: StreamDef | None = None
    interrupt: InterruptDef | bool | None = None
    history: HistoryDef | bool | None = None
    attachments: AgentAttachmentsDef | str | bool | None = None
    output_schema: str | None = None

    @model_validator(mode="after")
    def _normalize_interrupt(self) -> AgentDef:
        """Normalize ``interrupt: true`` shorthand to a full InterruptDef."""
        if self.interrupt is True:
            self.interrupt = InterruptDef()
        return self

    @model_validator(mode="after")
    def _normalize_history(self) -> AgentDef:
        """Normalize ``history: true|false`` shorthand to a full HistoryDef."""
        if self.history is True:
            self.history = HistoryDef()
        elif self.history is False:
            self.history = HistoryDef(enabled=False)
        return self

    @model_validator(mode="after")
    def _normalize_attachments(self) -> AgentDef:
        """Normalize ``attachments:`` shorthand to a full AgentAttachmentsDef.

        Accepts ``none``/``false`` (excluded), ``reference`` (manifest only),
        ``inline``/``true`` (manifest + inline media), or a full mapping. Leaves
        ``None`` untouched so the agent inherits the global default.
        """
        att = self.attachments
        if att is None or isinstance(att, AgentAttachmentsDef):
            return self
        if att is False or att == "none":
            self.attachments = AgentAttachmentsDef(enabled=False)
        elif att is True or att == "inline":
            self.attachments = AgentAttachmentsDef(enabled=True, inline=True)
        elif att == "reference":
            self.attachments = AgentAttachmentsDef(enabled=True, inline=False)
        else:
            raise ValueError(
                f"Invalid attachments value {att!r}. Use 'none', 'reference', "
                "'inline', a boolean, or a mapping like {inline: true}."
            )
        return self


# --- Orchestration Models --- #


class DelegateConnectionDef(BaseModel):
    """A delegation connection: orchestrator calls agent as a tool."""

    agent: str
    description: str


class DelegateOrchestrationDef(BaseModel):
    """Delegate mode: entry agent calls other agents as tools.

    A **new** Agent is constructed from the ``entry_name`` agent's blueprint
    (model, system_prompt, hooks, tools, etc.) with delegate tools added for
    each connection. The original agent is never mutated.

    Entry point is explicit via ``entry_name`` (consistent with swarm/graph).

    ``agent_kwargs`` is **merged** over the entry agent's ``agent_kwargs`` —
    orchestration values win on conflict, unset keys are inherited.
    """

    mode: Literal["delegate"] = "delegate"
    entry_name: str
    connections: list[DelegateConnectionDef]
    session_manager: SessionManagerDef | None = None
    hooks: list[HookDef | str] = Field(default_factory=list)
    agent_kwargs: dict[str, Any] = Field(default_factory=dict)
    """Merged over the entry agent's ``agent_kwargs`` (orchestration wins).

    Common uses: ``system_prompt``, ``callback_handler``, ``conversation_manager``.
    """

    @classmethod
    def reference_fields(cls) -> dict[str, str]:
        """Return mapping of JSON paths to reference types for name sanitization."""
        return {
            "entry_name": "node",
            "connections[].agent": "node",
        }


class SwarmOrchestrationDef(BaseModel):
    """Swarm mode: collaborative handoffs between peer agents.

    Agents transfer control to each other via handoff_to_agent tool.
    Uses strands Swarm under the hood.

    ``chat_output`` names the member agent whose streamed text becomes the
    assistant reply when the swarm is a first-class AG-UI entry. When omitted,
    the adapter emits the final active node's text once the run completes.
    """

    mode: Literal["swarm"] = "swarm"
    agents: list[str]
    entry_name: str
    chat_output: str | None = None
    max_handoffs: int = 20
    max_iterations: int = 20
    execution_timeout: float = 900.0
    node_timeout: float = 300.0
    session_manager: SessionManagerDef | None = None
    hooks: list[HookDef | str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_chat_output(self) -> SwarmOrchestrationDef:
        """Ensure ``chat_output`` (when set) names a member agent."""
        if self.chat_output is not None and self.chat_output not in self.agents:
            raise ValueError(
                f"swarm chat_output '{self.chat_output}' is not one of the swarm "
                f"agents: {sorted(self.agents)}."
            )
        return self

    @classmethod
    def reference_fields(cls) -> dict[str, str]:
        """Return mapping of JSON paths to reference types for name sanitization."""
        return {
            "entry_name": "node",
            "chat_output": "node",
            "agents[]": "node",
        }


class GraphEdgeDef(BaseModel):
    """An edge in a graph orchestration."""

    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    condition: str | None = None
    model_config = {"populate_by_name": True}


class GraphOrchestrationDef(BaseModel):
    """Graph mode: DAG-based orchestration with conditional edges.

    Agents execute in parallel batches based on dependency order.
    Uses strands Graph under the hood.

    ``chat_output`` names the node whose streamed text becomes the assistant
    reply when the graph is a first-class AG-UI entry. When omitted, the
    adapter streams the sole terminal node (a node with no outgoing edges) if
    there is exactly one, otherwise it emits the final node's text on
    completion.
    """

    mode: Literal["graph"] = "graph"
    edges: list[GraphEdgeDef]
    max_node_executions: int | None = None
    execution_timeout: float | None = None
    node_timeout: float | None = None
    reset_on_revisit: bool = False
    session_manager: SessionManagerDef | None = None
    entry_name: str
    chat_output: str | None = None
    hooks: list[HookDef | str] = Field(default_factory=list)

    def node_ids(self) -> set[str]:
        """Return every node id referenced by this graph (entry + edge ends)."""
        ids = {self.entry_name}
        for edge in self.edges:
            ids.add(edge.from_agent)
            ids.add(edge.to_agent)
        return ids

    def terminal_nodes(self) -> list[str]:
        """Return nodes with no outgoing edge (candidate chat-output nodes)."""
        sources = {edge.from_agent for edge in self.edges}
        return sorted(n for n in self.node_ids() if n not in sources)

    @model_validator(mode="after")
    def _validate_chat_output(self) -> GraphOrchestrationDef:
        """Ensure ``chat_output`` (when set) names a graph node."""
        if self.chat_output is not None and self.chat_output not in self.node_ids():
            raise ValueError(
                f"graph chat_output '{self.chat_output}' is not a node in the "
                f"graph: {sorted(self.node_ids())}."
            )
        return self

    @classmethod
    def reference_fields(cls) -> dict[str, str]:
        """Return mapping of JSON paths to reference types for name sanitization."""
        return {
            "entry_name": "node",
            "chat_output": "node",
            "edges[].from": "node",
            "edges[].to": "node",
        }


OrchestrationDef = Annotated[
    DelegateOrchestrationDef | SwarmOrchestrationDef | GraphOrchestrationDef,
    Field(discriminator="mode"),
]


# Sections that hold named dict collections (merged across config sources).
# IMPORTANT: these must exactly match the dict field names on AppConfig below.
COLLECTION_KEYS = ("models", "mcp_servers", "mcp_clients", "agents", "orchestrations")

# Groups of sections that share a lookup namespace — names must be unique within each group.
# mcp_servers / mcp_clients are independent namespaces and intentionally excluded.
JOINT_NAMESPACES: tuple[tuple[str, ...], ...] = (("agents", "orchestrations"),)


class AppConfig(BaseModel):
    """Root YAML configuration.

    Orchestrations are defined as a dict of named orchestration blocks
    that can reference each other for arbitrary nesting.
    """

    # IF YOU ADD A NEW SECTION, UPDATE:
    # 1. COLLECTION_KEYS above (if it's a named dict collection)
    # 2. JOINT_NAMESPACES above (if it shares a namespace with another section)

    version: str = "1"
    """Schema version — omit to use the default ``"1"``."""
    models: dict[str, ModelDef] = Field(default_factory=dict)
    mcp_servers: dict[str, MCPServerDef] = Field(default_factory=dict)
    mcp_clients: dict[str, MCPClientDef] = Field(default_factory=dict)
    agents: dict[str, AgentDef] = Field(default_factory=dict)
    session_manager: SessionManagerDef | None = None
    orchestrations: dict[str, OrchestrationDef] = Field(default_factory=dict)
    entry: str
    history: bool = False
    """Global default for per-agent ``history:``.

    Applies to any agent that does not set its own ``history:``. Defaults to
    ``False`` so sub-agents are stateless per run unless opted in. The entry
    agent is unaffected — its transcript is always the CopilotKit chat.
    """
    attachments: AttachmentsDef = Field(default_factory=AttachmentsDef)
    """Global reference/attachment policy (manifest + optional resolver tool)."""
    runtime: RuntimeDef = Field(default_factory=RuntimeDef)
    """Runtime behavior toggles (per-invocation agent overrides, …)."""
    telemetry: TelemetryDef = Field(default_factory=TelemetryDef)
    """Opt-in OpenTelemetry tracing (see :class:`TelemetryDef`)."""
    log_level: str = "WARNING"

    @model_validator(mode="after")
    def _validate_entry_ref(self) -> AppConfig:
        """Ensure entry references a defined agent or orchestration."""
        valid_names = set(self.agents) | set(self.orchestrations)
        if self.entry not in valid_names:
            raise ValueError(
                f"entry '{self.entry}' is not defined under agents: or orchestrations:.\n"
                f"Available: {', '.join(sorted(valid_names)) or '(none)'}"
            )
        return self

    @model_validator(mode="after")
    def _validate_no_name_collisions(self) -> AppConfig:
        """Ensure no name collisions within shared namespaces (see :data:`JOINT_NAMESPACES`)."""
        for namespace in JOINT_NAMESPACES:
            entries = [(key, set(getattr(self, key))) for key in namespace]
            for i, (section_a, names_a) in enumerate(entries):
                for section_b, names_b in entries[i + 1 :]:
                    overlap = names_a & names_b
                    if overlap:
                        raise ValueError(
                            f"Name collision between {section_a} and {section_b}: "
                            f"{sorted(overlap)}.\n"
                            f"Names must be unique within each section."
                        )
        return self
