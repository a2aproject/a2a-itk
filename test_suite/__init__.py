import itertools

from pyproto import instruction_pb2

from test_suite.agent_table import AgentTable
from test_suite.transports import ALL_TRANSPORTS as _ALL_TRANSPORTS
from test_suite.versions import parse_spec_version

_END_OF_TRAVERSAL_TOKEN = 'traversal-completed'  # noqa: S105

# The behaviors that subscribe again. The second one also resumes the new
# subscription from the last generation the caller saw. Resuming needs A2A
# 1.1, so on a call made at an older version the two are the same.
_RESUBSCRIBE = ('resubscribe', 'resubscribe_from_generation')


def _since_1_1(version: str | None) -> bool:
    """Is ``version`` A2A 1.1 or later?"""
    return version is not None and parse_spec_version(version) >= (1, 1)


def _speaks_1_1(agents: AgentTable, agent_id: str) -> bool:
    """Does the agent speak A2A 1.1 or newer?

    Only such an agent reads the fields of an instruction that are about the
    A2A version of a call. No step that an older agent carries out sets one.
    An older agent still passes on the ones in the instruction it hands to
    the next agent.
    """
    return _since_1_1(agents.protocol_versions(agent_id)[0])


def _call_version(
    agents: AgentTable, caller: str, callee: str, forced: str | None
) -> str | None:
    """The A2A version the call from ``caller`` to ``callee`` is made at.

    The forced one when the caller reads it from the instruction, and
    otherwise the newest version both speak, which is what a caller
    negotiates. ``None`` when they speak no version in common: the call
    cannot be made at all.
    """
    if forced and _speaks_1_1(agents, caller):
        return forced
    theirs = agents.protocol_versions(callee)
    return next((v for v in agents.protocol_versions(caller) if v in theirs), None)


def _served(
    agents: AgentTable, circuit: list[str], position: int, forced: str | None
) -> str | None:
    """The A2A version the agent at ``position`` of the circuit reports.

    That is the version it is called at. ``None`` when the agent is not asked
    to report: the harness calls the first agent itself, and an agent that
    does not speak A2A 1.1 does not read the request to report.
    """
    callee = circuit[position]
    if position == 0 or not _speaks_1_1(agents, callee):
        return None
    return _call_version(agents, circuit[position - 1], callee, forced)


def _say(
    response: instruction_pb2.ReturnResponse, text: str, served: str | None
) -> str:
    """Make ``response`` say ``text``; the token the agent is expected to say.

    With ``served``, the agent is also asked to report the A2A version it is
    called at, and the token ends in that version.
    """
    response.response = text
    if served is None:
        return text
    response.report_protocol_version = True
    return f'{text} served:{served}'


def _parse_edge_strings(
    edges: list[str], ref_sdks: list[str]
) -> list[tuple[str, str]]:
    """Parses a list of edge strings like ["0->1"] into SDK pairs.

    Args:
        edges: List of strings in "u_idx->v_idx" format.
        ref_sdks: The list of SDKs mapping to the indices.

    Returns:
        list[tuple[str, str]]: Parsed SDK pairs.

    Raises:
        ValueError: If edge format or index is invalid.
    """
    parsed = []
    expected_edge_parts = 2
    for edge_str in edges:
        parts = edge_str.split('->')
        if len(parts) != expected_edge_parts:
            raise ValueError(f'Invalid edge format: {edge_str}')
        u_s, v_s = parts[0].strip(), parts[1].strip()
        if not (u_s.isdigit() and v_s.isdigit()):
            raise ValueError(f'Invalid edge format or index: {edge_str}')
        u_idx, v_idx = int(u_s), int(v_s)
        if (
            u_idx < 0
            or u_idx >= len(ref_sdks)
            or v_idx < 0
            or v_idx >= len(ref_sdks)
        ):
            raise ValueError(f'Invalid edge format or index: {edge_str}')
        parsed.append((ref_sdks[u_idx], ref_sdks[v_idx]))
    return parsed


def _decompose_into_components(
    nodes_with_edges: list[str], adj: dict[str, list[str]]
) -> list[list[str]]:
    """Decomposes nodes with edges into connected components (undirected view)."""
    undirected_adj = {u: set() for u in adj}
    for u, neighbors in adj.items():
        for v in neighbors:
            undirected_adj[u].add(v)
            undirected_adj[v].add(u)

    visited = set()
    components = []
    for node in nodes_with_edges:
        if node not in visited:
            component = []
            stack = [node]
            visited_component = {node}
            while stack:
                u = stack.pop()
                component.append(u)
                for v in undirected_adj[u]:
                    if v not in visited_component:
                        visited_component.add(v)
                        stack.append(v)
            components.append(component)
            visited.update(visited_component)
    return components


def _verify_eulerian_graph(
    in_degree: dict[str, int],
    out_degree: dict[str, int],
    all_sdks: list[str],
) -> None:
    """Verifies that the graph defined by degrees and adjacency list is Eulerian.

    Args:
        adj: Adjacency list.
        in_degree: In-degree of each node.
        out_degree: Out-degree of each node.
        all_sdks: List of all SDKs.
        current_sdk: Starting SDK.

    Raises:
        ValueError: If graph is not Eulerian.
    """
    # 1. Verify Balance (In-degree == Out-degree)
    for node in all_sdks:
        if in_degree[node] != out_degree[node]:
            raise ValueError(
                f"Eulerian cycle impossible: Node '{node}' has in-degree={in_degree[node]} "
                f'and out-degree={out_degree[node]}.'
            )

    # 2. Strong Connectedness verification is deferred to component decomposition
    # in the traversal logic. All components must locally satisfy Eulerian conditions
    # (checked in step 1), making them independently traversable.


def _traversal_to_instruction(  # noqa: PLR0913
    circuit: list[str],
    transport: str,
    agents: AgentTable,
    streaming: bool = False,
    behavior: str = 'send_message',
    notification_server_url: str = '',
    hop_protocol_version: str | None = None,
) -> tuple[instruction_pb2.Instruction, list[str]]:
    """Converts a circuit of SDK names into a nested A2A instruction.

    Args:
        circuit: Ordered list of SDK names representing the traversal path.
        transport: The transport protocol to use for hops.
        agents: Where this run's agents are listening, and what they speak.
        streaming: Whether to use streaming.
        behavior: The behavior to set in CallAgent.
        notification_server_url: The URL of the notification server.
        hop_protocol_version: The A2A version to force every hop to.

    Returns:
        The nested instruction, and the tokens the agents are expected to
        say: the trace tokens, then the token that ends the traversal.
    """
    current_inst = instruction_pb2.Instruction()
    end_token = _say(
        current_inst.return_response,
        f'{_END_OF_TRAVERSAL_TOKEN}:{transport}',
        _served(agents, circuit, len(circuit) - 1, hop_protocol_version),
    )
    hold_task = behavior in _RESUBSCRIBE
    current_inst.return_response.hold_task = hold_task
    trace_tokens = []

    for i in range(len(circuit) - 2, -1, -1):
        u = circuit[i]
        v = circuit[i + 1]

        hop = instruction_pb2.Instruction()
        hop.steps.response_generator = (
            instruction_pb2.SeriesOfSteps.RESPONSE_GENERATOR_CONCAT
        )

        trace = hop.steps.instructions.add()
        # `u` says this token, so the version with it is the one `u` is
        # called at, not the version of the call from `u` to `v`.
        trace_tokens.append(
            _say(
                trace.return_response,
                f'[{u} -> {v} ({transport})]',
                _served(agents, circuit, i, hop_protocol_version),
            )
        )
        trace.return_response.hold_task = hold_task

        call_step = hop.steps.instructions.add()

        call_step.call_agent.agent_card_uri = agents.card_uri(v)
        call_step.call_agent.transport = transport
        call_step.call_agent.streaming = streaming
        call_step.call_agent.instruction.CopyFrom(current_inst)

        # Two fields ask the calling agent for something A2A 1.1 added to a
        # request. They are set only on a call made at 1.1 or later, which
        # also means that the agent making it reads them.
        version = _call_version(agents, u, v, hop_protocol_version)

        if behavior == 'push_notification':
            call_step.call_agent.push_notification.CopyFrom(
                instruction_pb2.PushNotificationBehavior(
                    url=notification_server_url
                )
            )
            if _since_1_1(version):
                call_step.call_agent.push_notification.protocol_version = version
        elif behavior == 'send_message':
            call_step.call_agent.send_message.CopyFrom(
                instruction_pb2.SendMessageBehavior()
            )
        elif behavior in _RESUBSCRIBE:
            call_step.call_agent.resubscribe.CopyFrom(
                instruction_pb2.ResubscribeBehavior()
            )
            if behavior == 'resubscribe_from_generation' and _since_1_1(version):
                call_step.call_agent.resubscribe.from_last_seen_generation = True
        if hop_protocol_version and _speaks_1_1(agents, u):
            call_step.call_agent.protocol_version = hop_protocol_version

        current_inst = hop

    return current_inst, [*trace_tokens, end_token]


def create_test_suite(  # noqa: PLR0913
    sdks: list[str],
    agents: AgentTable,
    edges: list[str] | None = None,
    protocols: list[str] | None = None,
    streaming: bool = False,
    behavior: str = 'send_message',
    notification_server_url: str = '',
    hop_protocol_version: str | None = None,
) -> tuple[
    instruction_pb2.Instruction,
    list[str],
]:
    """Builds the nested instruction for one scenario, and its trace tokens.

    An agent that speaks A2A 1.1 is asked to report the version it is called
    at, and each of its tokens then ends in ``served:<version>``. An agent
    that speaks nothing newer than A2A 1.0 is asked nothing about versions,
    so a traversal among such agents is the instruction it was before those
    fields existed.

    A call made at A2A 1.1 also names that version for the push notification
    config it registers, and with ``resubscribe_from_generation`` it resumes
    the second subscription from the last generation the caller saw.

    Args:
        sdks: Agent identifiers taking part, traversal start first.
        agents: Where this run's agents are listening, and what they speak.
            Every identifier in ``sdks`` must be present or the traversal
            cannot be addressed.
        edges: Optional explicit edges; a complete digraph when omitted.
        protocols: Transports to build a circuit for; all three when omitted.
        streaming: Whether hops stream.
        behavior: What each hop asks the next agent to do: ``send_message``,
            ``push_notification``, ``resubscribe`` or
            ``resubscribe_from_generation``.
        notification_server_url: Where push notifications are POSTed.
        hop_protocol_version: The A2A version every call is made at, for the
            agents that speak A2A 1.1. ``None`` leaves each call to the
            version its two agents negotiate.
    """
    testing_instruction = instruction_pb2.Instruction()
    testing_instruction.steps.response_generator = (
        instruction_pb2.SeriesOfSteps.RESPONSE_GENERATOR_CONCAT
    )

    parsed_edges = _parse_edge_strings(edges, sdks) if edges else None

    transports_to_test = (
        protocols if protocols is not None else list(_ALL_TRANSPORTS)
    )

    expected_end_tokens = []
    for transport in transports_to_test:
        circuits = _euler_traversal_with_hierholzer(
            sdks[0],
            sdks,
            edges=parsed_edges,
        )
        for circuit in circuits:
            instruction_for_transport, tokens = _traversal_to_instruction(
                circuit,
                transport,
                agents,
                streaming=streaming,
                behavior=behavior,
                notification_server_url=notification_server_url,
                hop_protocol_version=hop_protocol_version,
            )
            expected_end_tokens.extend(tokens)
            testing_instruction.steps.instructions.add().CopyFrom(
                instruction_for_transport
            )

    return (
        testing_instruction,
        expected_end_tokens,
    )


def _euler_traversal_with_hierholzer(
    current_sdk: str,
    all_sdks: list[str],
    edges: list[tuple[str, str]] | None = None,
) -> list[str]:
    """
    This function utilizes Hierholzer's Algorithm to find an Eulerian Circuit
    covering all directed edges exactly once.

    Why this is guaranteed to exist for a Complete Digraph (Complete
    Directed Graph with no self-loops):
    1. **Connectedness**: Direct edges exist between every node pair, making it
       strongly connected.
    2. **Balance (In-degree = Out-degree)**: In a graph of N vertices, every
       node has exactly (N-1) outgoing edges and (N-1) incoming edges.
    A strongly connected graph where In(X) = Out(X) for all nodes is guaranteed
    to possess an Eulerian Circuit, traversing all segments linearly without
    duplicate activation overlaps.

    **Note on DFS vs Eulerian traversal**:
    Standard Depth-First Search (DFS) is vertex-focused (it visits vertices and
    marks them as visited to avoid cycles). In a graph with multiple edges or
    cycles, a standard DFS will skip edges that lead to already-visited nodes
    (such as back-edges or cross-edges). Consequently, DFS may skip edges and
    fail to produce an Eulerian traversal. Hierholzer's algorithm, in contrast,
    consumes *edges* to ensure that every directed edge is visited exactly once.

    Args:
        current_sdk: The starting agent/SDK node.
        all_sdks: The list of ALL agents/SDKs in the graph.
        edges: Optional list of pre-parsed SDK pairs (u, v).
    Returns:
        list[str]: The node circuit representing the traversal path.
    """

    # 1. Generate Edges (Custom or Complete Digraph)
    if edges:
        valid_nodes = set(all_sdks)
        target_edges = [
            (u, v) for u, v in edges if u in valid_nodes and v in valid_nodes
        ]
    else:
        target_edges = [(u, v) for u in all_sdks for v in all_sdks if u != v]

    # 2. Build Adjacency List and Calculate Degrees
    adj = {u: [] for u in all_sdks}
    in_degree = dict.fromkeys(all_sdks, 0)
    out_degree = dict.fromkeys(all_sdks, 0)
    for u, v in target_edges:
        adj[u].append(v)
        out_degree[u] += 1
        in_degree[v] += 1

    # 3. Verify Eulerian Balance existence
    _verify_eulerian_graph(in_degree, out_degree, all_sdks)

    # 4. Decompose into components and run Hierholzer for each
    nodes_with_edges = [n for n in all_sdks if out_degree[n] > 0]
    components = _decompose_into_components(nodes_with_edges, adj)
    circuits = []

    if not components:
        return [[current_sdk]]

    # Sort to prioritize component containing current_sdk (execute first)
    components.sort(key=lambda c: current_sdk not in c)

    for comp in components:
        start_node = current_sdk if current_sdk in comp else comp[0]
        stack = [start_node]
        comp_circuit = []
        while stack:
            u = stack[-1]
            if adj[u]:
                v = adj[u].pop()
                stack.append(v)
            else:
                comp_circuit.append(stack.pop())
        comp_circuit.reverse()
        circuits.append(comp_circuit)

    return circuits


def _map_edges_to_subgraph(
    original_edges: list[str], original_sdks: list[str], sub_sdks: list[str]
) -> list[str]:
    """Maps original edge strings to new indices in the sub_sdks subgraph."""
    name_to_new_idx = {name: idx for idx, name in enumerate(sub_sdks)}
    mapped_edges = []
    expected_edge_parts = 2
    for edge_str in original_edges:
        parts = edge_str.split('->')
        if len(parts) != expected_edge_parts:
            raise ValueError(f'Invalid edge format: {edge_str}')
        u_s, v_s = parts[0].strip(), parts[1].strip()
        if not (u_s.isdigit() and v_s.isdigit()):
            raise ValueError(f'Invalid edge format or index: {edge_str}')
        u_idx, v_idx = int(u_s), int(v_s)
        if u_idx < 0 or u_idx >= len(original_sdks) or v_idx < 0 or v_idx >= len(original_sdks):
            raise ValueError(f'Invalid edge index: {edge_str}')

        u_name = original_sdks[u_idx]
        v_name = original_sdks[v_idx]

        if u_name in name_to_new_idx and v_name in name_to_new_idx:
            new_u = name_to_new_idx[u_name]
            new_v = name_to_new_idx[v_name]
            mapped_edges.append(f'{new_u}->{new_v}')

    return mapped_edges


def _get_valid_subgraphs(  # noqa: PLR0913
    sdks: list[str],
    edges: list[str] | None,
    behavior: str,
    agents: AgentTable,
    protocols: list[str] | None = None,
    streaming: bool = False,
) -> list[dict]:
    """Generates all valid traversable induced subgraphs containing the root node of size >= 2.

    Traversability is decided by trying to build the instruction and seeing
    whether it raises. Only ``ValueError`` counts as "not traversable" — a
    missing agent raises ``RuntimeError`` from :meth:`AgentTable.card_uri`
    and propagates, so a peer that failed to start can't be mistaken for an
    unbalanced subgraph and silently dropped.
    """
    if not sdks:
        return []

    root = sdks[0]
    remaining = sdks[1:]

    valid_subgraphs = []

    # Generate subsets of remaining nodes
    for r in range(len(remaining) + 1):
        for combo in itertools.combinations(remaining, r):
            sub_sdks = [root] + list(combo)

            if len(sub_sdks) < 2:
                continue

            # Map custom edges
            new_edges = None
            if edges is not None:
                try:
                    new_edges = _map_edges_to_subgraph(edges, sdks, sub_sdks)
                except ValueError:
                    continue

            # Validate traversability
            try:
                create_test_suite(
                    sdks=sub_sdks,
                    agents=agents,
                    edges=new_edges,
                    protocols=protocols,
                    streaming=streaming,
                    behavior=behavior,
                )
            except ValueError:
                # Invalid subgraph, skip
                continue

            valid_subgraphs.append({
                'sdks': sub_sdks,
                'edges': new_edges,
            })

    return valid_subgraphs

