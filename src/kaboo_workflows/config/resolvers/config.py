"""ResolvedConfig, ResolvedInfra, and resolve_infra orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...manifest import build_manifest
from ...mcp.lifecycle import MCPLifecycle
from ...wire import make_event_queue
from .mcp import resolve_mcp_client, resolve_mcp_server
from .models import resolve_model

if TYPE_CHECKING:
    from strands import Agent
    from strands.models import Model
    from strands.tools.mcp import MCPClient as StrandsMCPClient

    from ...mcp.server import MCPServer
    from ...types import Node
    from ...wire import EventQueue
    from ..schema import AppConfig

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class ResolvedConfig:
    """Fully resolved config — lifecycle started, agents ready.

    After calling :func:`~kaboo_workflows.config.loaders.load`, use
    :meth:`wire_event_queue` to set up event streaming::

        resolved = load("config.yaml")
        event_queue = resolved.wire_event_queue()
    """

    agents: dict[str, Agent] = field(default_factory=dict)
    orchestrators: dict[str, Node] = field(default_factory=dict)
    entry: Node
    mcp_lifecycle: MCPLifecycle = field(default_factory=MCPLifecycle)
    app_config: AppConfig | None = None

    def wire_event_queue(
        self,
        *,
        session_id: str | None = None,
        tool_labels: dict[str, str] | None = None,
    ) -> EventQueue:
        """Wire all agents and orchestrators for event streaming.

        This is the recommended way to set up event streaming.  It:

        1. Builds a :class:`~kaboo_workflows.types.SessionManifest` from the
           resolved runtime objects.
        2. Auto-resolves stream groups from the original AppConfig so every
           agent is attributed to a hierarchical dot-path group.
        3. Collects tool labels from MCP clients and agents in the original
           AppConfig (agent-level overrides MCP-level).
        4. Wires every agent (and orchestrator) with an
           :class:`~kaboo_workflows.hooks.EventPublisher` via
           :func:`~kaboo_workflows.wire.make_event_queue`.
        5. Emits a SESSION_START event carrying the manifest as the first
           event on the queue.

        .. warning::

            This **mutates** the agents and orchestrators stored on this
            instance by adding hooks and overwriting ``callback_handler``.
            Call it only once per ``ResolvedConfig`` instance.

        Args:
            session_id: Optional session ID to embed in events.
            tool_labels: Optional runtime tool name → display label mapping.
                These take highest priority, overriding both agent-level and
                MCP-client-level labels from YAML config.

        Returns:
            A ready-to-use :class:`~kaboo_workflows.wire.EventQueue` with
            SESSION_START already on it.

        Raises:
            ValueError: If the entry node cannot be resolved by object identity.
        """
        manifest = build_manifest(self.agents, self.orchestrators, self.entry)
        stream_groups = _auto_resolve_stream_groups(self.app_config)
        history = _resolve_history(self.app_config)
        chat_owner = _resolve_chat_owner(self.app_config)
        chat_reply = chat_owner or _resolve_chat_output(self.app_config)
        merged_labels = _collect_tool_labels(self.app_config)
        if tool_labels:
            merged_labels.update(tool_labels)
        event_queue = make_event_queue(
            self.agents,
            orchestrators=self.orchestrators,
            tool_labels=merged_labels or None,
            stream_groups=stream_groups,
            history=history,
            entry_name=manifest.entry.name,
            chat_owner=chat_owner,
            chat_reply=chat_reply,
            session_id=session_id,
        )
        event_queue.emit_session_start(manifest)
        return event_queue


def _auto_resolve_stream_groups(
    app_config: AppConfig | None,
) -> dict[str, tuple[str, str]]:
    """Build stream group assignments for all agents from AppConfig.

    Every orchestration mode gets dot-path hierarchies so the UI can drill
    through all levels: delegate connections, swarm members, and graph nodes
    each nest under their orchestration's path. Nested orchestrations (any
    mode inside any mode — swarm-in-graph, graph-in-graph, delegate targeting
    a swarm/graph) get fully composed dot-paths. Every agent is assigned a
    group even without explicit ``stream:`` config.
    """
    from ..schema import (
        DelegateOrchestrationDef,
        GraphOrchestrationDef,
        SwarmOrchestrationDef,
    )
    from .orchestrations.planner import topological_sort

    if app_config is None:
        return {}

    groups: dict[str, tuple[str, str]] = {}
    orch_members: dict[str, list[str]] = {}

    def _agent_label(name: str) -> tuple[str, str]:
        agent_def = app_config.agents.get(name)  # type: ignore[union-attr]
        group = name
        title = name.replace("_", " ").title()
        if agent_def and agent_def.stream:
            if agent_def.stream.group:
                group = agent_def.stream.group
            if agent_def.stream.title:
                title = agent_def.stream.title
        return group, title

    def _nest_orchestration(prefix: str, member: str, members: list[str]) -> None:
        """Re-prefix a nested orchestration's whole subtree under *prefix*.

        Handles both grouping conventions. Swarm/graph members embed their
        orchestration name in their base path (``team.triage``) and parent to the
        orchestration group, which is emitted — so they are simply re-rooted under
        *prefix*. A delegate member does NOT get its own segment: its entry agent
        runs AS the orchestration (the forked manager publishes under the
        orchestration group), so the entry COLLAPSES into ``prefix.member`` and its
        connections re-root there. Otherwise the entry's blueprint group is a
        phantom that never emits, and the entry's children are orphaned (their
        parent id points at a group that doesn't exist, so they're invisible when
        drilling into the orchestration).
        """
        member_def = app_config.orchestrations.get(member)
        entry = member_def.entry_name if isinstance(member_def, DelegateOrchestrationDef) else None
        target = f"{prefix}.{member}"
        groups[member] = (target, member.replace("_", " ").title())
        members.append(member)
        for inner in orch_members.get(member, []):
            old_group, old_title = groups.get(inner, (inner, inner.replace("_", " ").title()))
            if entry is not None and (old_group == entry or old_group.startswith(f"{entry}.")):
                new_group = target + old_group[len(entry) :]
            elif old_group == member or old_group.startswith(f"{member}."):
                new_group = f"{prefix}.{old_group}"
            else:
                new_group = f"{prefix}.{member}.{old_group}"
            groups[inner] = (new_group, old_title)
            members.append(inner)

    build_order = topological_sort(app_config.orchestrations)

    for orch_name in build_order:
        orch_def = app_config.orchestrations[orch_name]

        if isinstance(orch_def, (SwarmOrchestrationDef, GraphOrchestrationDef)):
            if isinstance(orch_def, SwarmOrchestrationDef):
                member_names = list(orch_def.agents)
            else:
                member_names = sorted(orch_def.node_ids())
            members: list[str] = []
            for member in member_names:
                if member in app_config.orchestrations:
                    _nest_orchestration(orch_name, member, members)
                else:
                    child_group, child_title = _agent_label(member)
                    groups[member] = (f"{orch_name}.{child_group}", child_title)
                    members.append(member)
            orch_members[orch_name] = members
            continue

        if not isinstance(orch_def, DelegateOrchestrationDef):
            continue

        entry_group, entry_title = _agent_label(orch_def.entry_name)
        groups[orch_def.entry_name] = (entry_group, entry_title)
        members = [orch_def.entry_name]

        for conn in orch_def.connections:
            child_group, child_title = _agent_label(conn.agent)
            conn_is_orch = conn.agent in app_config.orchestrations

            if conn_is_orch:
                orch_group_path = f"{entry_group}.{conn.agent}"
                orch_title = conn.agent.replace("_", " ").title()
                orch_cfg = app_config.orchestrations[conn.agent]
                if isinstance(orch_cfg, DelegateOrchestrationDef):
                    orch_def_inner = orch_cfg
                    inner_entry_def = app_config.agents.get(orch_def_inner.entry_name)
                    if inner_entry_def and inner_entry_def.stream and inner_entry_def.stream.title:
                        orch_title = inner_entry_def.stream.title
                groups[conn.agent] = (orch_group_path, orch_title)
                members.append(conn.agent)

                # Re-root the connected orchestration's subtree under its group.
                # A delegate connection's entry agent collapses into the
                # orchestration group (it runs AS the orchestration), so replace
                # its leading entry segment rather than appending one — otherwise
                # its children orphan onto a phantom `<path>.<entry>` group. A
                # swarm/graph connection already embeds its own name, so re-root it
                # under the delegating entry's group without doubling the segment.
                inner_entry = (
                    orch_cfg.entry_name if isinstance(orch_cfg, DelegateOrchestrationDef) else None
                )
                for inner_name in orch_members.get(conn.agent, []):
                    if inner_name in groups:
                        old_group, old_title = groups[inner_name]
                        if inner_entry is not None and (
                            old_group == inner_entry or old_group.startswith(f"{inner_entry}.")
                        ):
                            new_group = orch_group_path + old_group[len(inner_entry) :]
                        elif old_group == conn.agent or old_group.startswith(f"{conn.agent}."):
                            new_group = f"{entry_group}.{old_group}"
                        else:
                            new_group = f"{orch_group_path}.{old_group}"
                        groups[inner_name] = (new_group, old_title)
                        members.append(inner_name)
            else:
                groups[conn.agent] = (f"{entry_group}.{child_group}", child_title)
                members.append(conn.agent)

        orch_members[orch_name] = members

    for name, agent_def in app_config.agents.items():
        if name in groups:
            continue
        group = name
        title = name.replace("_", " ").title()
        if agent_def.stream:
            if agent_def.stream.group:
                group = agent_def.stream.group
            if agent_def.stream.title:
                title = agent_def.stream.title
        groups[name] = (group, title)

    return groups


def _resolve_history(
    app_config: AppConfig | None,
) -> dict[str, tuple[str, bool]]:
    """Resolve per-agent history keys and enablement.

    Returns ``{agent_name: (key, enabled)}`` where:

    - ``key`` is the agent's ``history.group`` (shared-transcript bucket) when
      set, else its stable dot-path stream group (reused from
      :func:`_auto_resolve_stream_groups`). Grouped agents therefore collapse
      to one key and share a transcript.
    - ``enabled`` is the agent's ``history.enabled`` when the agent declares
      ``history:``, otherwise the global ``AppConfig.history`` default.

    The entry agent is intentionally included but ignored at wire time — its
    transcript is always the CopilotKit chat, not the ``kaboo_history`` blob.
    """
    if app_config is None:
        return {}

    stream_groups = _auto_resolve_stream_groups(app_config)
    default_enabled = app_config.history

    history: dict[str, tuple[str, bool]] = {}
    for name, agent_def in app_config.agents.items():
        group, _title = stream_groups.get(name, (name, name))
        key = group
        enabled = default_enabled
        if agent_def.history is not None:
            enabled = agent_def.history.enabled
            if agent_def.history.group:
                key = agent_def.history.group
        history[name] = (key, enabled)
    return history


def _resolve_chat_owner(app_config: AppConfig | None) -> str | None:
    """Return the agent whose transcript IS the chat (never gets a HistoryHook).

    For a delegate entry, the running chat agent is a fork of the delegate's
    entry agent's blueprint. That blueprint agent is present in ``agents`` under
    its own name (e.g. ``coordinator``) — distinct from the orchestration name
    (``manifest.entry.name``) — and must not also carry a HistoryHook. Returns
    the blueprint's name so :func:`~kaboo_workflows.wire.make_event_queue` can
    skip it. For a plain-agent entry the orchestration/entry name already covers
    it; for swarm/graph there is no single chat-owning member agent.
    """
    from ..schema import DelegateOrchestrationDef

    if app_config is None:
        return None
    orch = app_config.orchestrations.get(app_config.entry)
    if isinstance(orch, DelegateOrchestrationDef):
        return orch.entry_name
    return None


def _resolve_chat_output(app_config: AppConfig | None) -> str | None:
    """Return the node whose streamed text is the chat reply for a swarm/graph entry.

    Used only when the entry orchestration is a first-class Swarm/Graph:

    - Explicit ``chat_output:`` wins.
    - For a graph with no explicit ``chat_output`` and exactly one terminal
      node (no outgoing edge), that terminal node is streamed live.
    - Otherwise ``None`` — the adapter emits the final node's text once the
      run completes (no live token streaming for the chat bubble).

    Returns ``None`` for plain-agent and delegate entries (their chat voice is
    the entry agent itself, handled by the standard AG-UI path).
    """
    from ..schema import GraphOrchestrationDef, SwarmOrchestrationDef

    if app_config is None:
        return None
    orch = app_config.orchestrations.get(app_config.entry)
    if isinstance(orch, SwarmOrchestrationDef):
        return orch.chat_output
    if isinstance(orch, GraphOrchestrationDef):
        if orch.chat_output is not None:
            return orch.chat_output
        terminals = orch.terminal_nodes()
        return terminals[0] if len(terminals) == 1 else None
    return None


def _collect_tool_labels(
    app_config: AppConfig | None,
) -> dict[str, str]:
    """Collect tool labels from MCP clients and agents in AppConfig.

    Merge priority (highest wins): agent-level > MCP-client-level.
    Runtime overrides are applied separately by the caller.
    """
    if app_config is None:
        return {}

    labels: dict[str, str] = {}

    for _client_name, client_def in app_config.mcp_clients.items():
        labels.update(client_def.tool_labels)

    for _agent_name, agent_def in app_config.agents.items():
        labels.update(agent_def.tool_labels)

    return labels


@dataclass
class ResolvedInfra:
    """Infrastructure resolved from config — lifecycle NOT started.

    This is the pure result of :func:`resolve_infra`.  Lifecycle is cold,
    agents are not yet created.

    Session managers are NOT stored here — they are built per agent and per
    orchestration at session time, from ``config.session_manager`` (the global
    def) plus ``effective_session_id`` computed by ``load_session``.

    Use :func:`~kaboo_workflows.config.loaders.load` for a fully
    activated system, or manually::

        infra = resolve_infra(config)
        infra.mcp_lifecycle.start()
        agents = resolve_agents(agent_defs=config.agents, ...)
    """

    models: dict[str, Model] = field(default_factory=dict)
    clients: dict[str, StrandsMCPClient] = field(default_factory=dict)
    mcp_lifecycle: MCPLifecycle = field(default_factory=MCPLifecycle)


def resolve_infra(config: AppConfig) -> ResolvedInfra:
    """Resolve infrastructure from an AppConfig (pure, no I/O).

    Creates model objects, MCP server/client objects, and a lifecycle
    manager.  Nothing is started.

    Resolution order:

    1. Models (no dependencies)
    2. MCP servers (no dependencies)
    3. MCP clients (depend on servers)
    4. MCP lifecycle (assembles servers + clients, **not** started)
    5. Session manager validation only — ``agentcore`` provider rejected
       globally; no instance is constructed (instances are built per-leaf
       at session time).

    Agents and orchestration are resolved in :func:`load` after
    ``mcp_lifecycle.start()`` because ``Agent.__init__`` auto-starts
    MCP clients which need servers to be running first.  The lifecycle
    start in ``load()`` is idempotent — the context manager is still
    used for graceful shutdown.

    Args:
        config: Parsed AppConfig from YAML.

    Returns:
        A :class:`ResolvedInfra` with models, clients, and a cold MCP lifecycle.
    """
    # Models
    models: dict[str, Model] = {}
    for name, model_def in config.models.items():
        models[name] = resolve_model(model_def)
        logger.info("model=<%s>, provider=<%s> | resolved model", name, model_def.provider)

    # MCP servers
    servers: dict[str, MCPServer] = {}
    for name, server_def in config.mcp_servers.items():
        servers[name] = resolve_mcp_server(server_def, name=name)
        logger.info("server=<%s> | resolved MCP server", name)

    # MCP clients (resolved but NOT started)
    clients: dict[str, StrandsMCPClient] = {}
    for name, client_def in config.mcp_clients.items():
        clients[name] = resolve_mcp_client(client_def, servers, name=name)
        logger.info("client=<%s> | resolved MCP client", name)

    # MCP lifecycle (cold — not started)
    lifecycle = MCPLifecycle()
    for name, server in servers.items():
        lifecycle.add_server(name, server)
    for name, client in clients.items():
        lifecycle.add_client(name, client)

    # Session manager — validation only
    # Instances are built per leaf in load_session / agents / orchestrations.
    # Provider 'agentcore' cannot be set globally -
    # it requires a unique 'actor_id' per agent. Fail fast at boot.
    if (
        config.session_manager is not None
        and config.session_manager.provider.lower() == "agentcore"
    ):
        raise ValueError(
            "The 'agentcore' session manager cannot be set globally.\n"
            "Configure it per-agent — 'actor_id' must be unique per agent."
        )

    return ResolvedInfra(
        models=models,
        clients=clients,
        mcp_lifecycle=lifecycle,
    )
