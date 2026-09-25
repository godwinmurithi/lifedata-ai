"""Semantic layout compiler for Superset `position_json` (spec §6 rule 7).

The model never emits `position_json`. It says *what it wants* — "make the
trend full width", "put the KPIs across the top" — and this module is the only
code in the copilot permitted to turn that into grid geometry. Two reasons the
boundary is drawn here rather than in the write tool:

* `position_json` is a flat, self-referential graph. A model that emits it
  directly will eventually emit a node whose `parents` disagree with its actual
  ancestry, or regenerate a `CHART-xxxx` id — which silently detaches the chart
  from the dashboard because Superset keys chart membership on that node id.
* Every geometry rule the POC cares about (12 columns, rows never overflowing,
  KPI tiles equal width) is enforceable in one place if only one place writes.

**The trap this module exists to survive** (verified against Superset 6.1.0 —
see `docs/superset-integration-findings.md` item 4): a freshly created
dashboard returns `position_json` as SQL `NULL`, surfacing as JSON `null`. Not
`{}`, not `"{}"`. `json.loads(None)` raises `TypeError`, so the naive path dies
on the very first dashboard the copilot creates. `parse_position_json` treats
"no layout yet" as a first-class case and synthesises a ROOT→GRID skeleton.

Usage from a write tool::

    from lifedata_ai.validation.layout import compile_operations

    position_json = compile_operations(
        dashboard["position_json"],           # may legitimately be None
        [{"operation": "make_full_width", "chart_id": 42}],
    )
    # PUT /api/v1/dashboard/{id} with {"position_json": position_json}

Guarantees the callers rely on:

* **Copy on write.** The input tree is never mutated; every operation returns a
  new `LayoutTree`. A failed operation therefore leaves the caller's tree
  untouched and safe to serialise unchanged.
* **Deterministic output.** Same input plus same operations produce a
  byte-identical string. New node ids are derived by hashing a stable seed
  rather than drawn from a random source, so snapshots and undo comparisons in
  `models.Snapshot` mean something.
* **No orphans.** Every operation is checked afterwards: a node reachable from
  `ROOT_ID` before must still be reachable, unless the operation removed it on
  purpose. This is what stops a botched move from quietly dropping a chart.

Errors are `LayoutError` and carry a sentence written for the model to read and
self-correct from, per the spec §5 loop (validation failures go back as the
tool result, not as a stack trace).
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

# --- Superset grid constants ---------------------------------------------
#
# Mirrors `superset-frontend/src/dashboard/util/constants.ts` at the pinned
# 6.1.0 tag. The bounds agree with the action schemas in
# `validation/schemas/actions.py` (`set_width` 1–12, `set_height` 1–100); if
# Superset ever changes them, both places must move together.

GRID_COLUMN_COUNT: Final[int] = 12
GRID_MIN_COLUMN_COUNT: Final[int] = 1
GRID_MIN_ROW_UNITS: Final[int] = 1
GRID_MAX_ROW_UNITS: Final[int] = 100
GRID_DEFAULT_CHART_WIDTH: Final[int] = 4
GRID_DEFAULT_CHART_HEIGHT: Final[int] = 50

#: Height for tiles gathered by `kpi_row`. Half the default chart height reads
#: as a compact banner rather than a row of full-size panels.
KPI_ROW_HEIGHT: Final[int] = 25

ROOT_ID: Final[str] = "ROOT_ID"
GRID_ID: Final[str] = "GRID_ID"
VERSION_KEY: Final[str] = "DASHBOARD_VERSION_KEY"
LAYOUT_VERSION: Final[str] = "v2"

#: Node types that may hold rows. `move_to_top` and `kpi_row` insert new rows
#: into the nearest one of these, so a chart living inside a tab keeps its tab.
CONTAINER_TYPES: Final[frozenset[str]] = frozenset({"GRID", "TAB"})

#: Containers that must survive even when empty — pruning them would break the
#: dashboard skeleton or delete a tab the user created.
UNPRUNABLE_TYPES: Final[frozenset[str]] = frozenset({"ROOT", "GRID", "TAB", "TABS", "HEADER"})


class LayoutError(ValueError):
    """A layout operation cannot be carried out.

    The message is deliberately a plain sentence naming the offending chart or
    operation: it is handed straight back to the model as a tool error so it
    can retry with a different target rather than guessing.
    """


# --- Tree ----------------------------------------------------------------


@dataclass
class LayoutTree:
    """A parsed `position_json`, split into layout nodes and everything else.

    `extras` matters more than it looks: Superset stores `DASHBOARD_VERSION_KEY`
    as a bare string alongside the node dicts in the same flat mapping. Dropping
    it on a round trip would downgrade the layout version, so unrecognised
    top-level entries are carried through verbatim.
    """

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> LayoutTree:
        """A deep copy — the unit of the copy-on-write guarantee."""
        return LayoutTree(nodes=copy.deepcopy(self.nodes), extras=copy.deepcopy(self.extras))

    def node(self, node_id: str) -> dict[str, Any]:
        """The node, or `LayoutError` naming the id that is missing."""
        try:
            return self.nodes[node_id]
        except KeyError:
            raise LayoutError(f"Layout node {node_id!r} does not exist on this dashboard.") from None

    def to_dict(self) -> dict[str, Any]:
        """The flat mapping Superset expects, nodes and extras recombined."""
        return {**self.extras, **copy.deepcopy(self.nodes)}

    def chart_ids(self) -> list[int]:
        """Superset slice ids present in the layout, in ascending order."""
        found = {
            node["meta"]["chartId"]
            for node in self.nodes.values()
            if node.get("type") == "CHART" and isinstance(node.get("meta"), dict)
            and isinstance(node["meta"].get("chartId"), int)
        }
        return sorted(found)


def empty_skeleton() -> LayoutTree:
    """The minimal valid layout: `ROOT_ID` → `GRID_ID`, no rows.

    Deliberately omits the `HEADER_ID` node Superset's editor adds. We do not
    know the dashboard title here, and Superset renders the header from
    `dashboard_title` regardless; inventing a header node with the wrong text
    would be worse than omitting it.
    """
    return LayoutTree(
        nodes={
            ROOT_ID: {"id": ROOT_ID, "type": "ROOT", "children": [GRID_ID]},
            GRID_ID: {
                "id": GRID_ID,
                "type": "GRID",
                "children": [],
                "parents": [ROOT_ID],
                "meta": {},
            },
        },
        extras={VERSION_KEY: LAYOUT_VERSION},
    )


def parse_position_json(raw: str | dict[str, Any] | None) -> LayoutTree:
    """Parse a dashboard's `position_json` into a `LayoutTree`.

    `None`, `""`, the string `"null"`, `{}` and `"{}"` all mean "this dashboard
    has no layout yet" and produce `empty_skeleton()`. That is not defensive
    padding: Superset 6.1.0 returns SQL `NULL` for a dashboard created through
    `POST /api/v1/dashboard/`, so this is the *normal* first call for any
    dashboard the copilot builds.

    Anything present but structurally inconsistent — a node keyed under an id
    that disagrees with its own `id`, a child reference to a node that does not
    exist — raises rather than being repaired. Silently patching a layout we do
    not understand risks writing back something worse than we read.
    """
    if raw is None:
        return empty_skeleton()

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return empty_skeleton()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LayoutError(f"The dashboard layout is not valid JSON: {exc}.") from exc
    else:
        decoded = raw

    if decoded is None:
        return empty_skeleton()
    if not isinstance(decoded, dict):
        raise LayoutError(
            "The dashboard layout must be a JSON object of node id to node, "
            f"got {type(decoded).__name__}."
        )
    if not decoded:
        return empty_skeleton()

    nodes: dict[str, dict[str, Any]] = {}
    extras: dict[str, Any] = {}
    for key, value in decoded.items():
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            nodes[key] = copy.deepcopy(value)
        else:
            extras[key] = copy.deepcopy(value)

    tree = LayoutTree(nodes=nodes, extras=extras)
    _validate_structure(tree)
    return tree


def serialise(tree: LayoutTree) -> str:
    """Render a tree back to a `position_json` string.

    Keys are sorted and separators are compact so that the same tree always
    serialises to the same bytes — undo comparisons and snapshot diffs would be
    meaningless otherwise. Child lists keep their order: that order *is* the
    layout.
    """
    return json.dumps(tree.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_structure(tree: LayoutTree) -> None:
    """Reject a layout we cannot reason about, naming what is wrong."""
    for node_id, node in tree.nodes.items():
        declared = node.get("id")
        if declared is not None and declared != node_id:
            raise LayoutError(
                f"Layout node keyed {node_id!r} declares id {declared!r}; the dashboard "
                "layout is inconsistent and cannot be edited safely."
            )
        children = node.get("children", [])
        if not isinstance(children, list):
            raise LayoutError(f"Layout node {node_id!r} has a non-list 'children'.")
        for child in children:
            if child not in tree.nodes:
                raise LayoutError(
                    f"Layout node {node_id!r} references a child {child!r} that does not "
                    "exist; the dashboard layout is inconsistent."
                )

    if ROOT_ID not in tree.nodes:
        raise LayoutError(
            f"The dashboard layout has no {ROOT_ID!r} node, so its structure cannot be "
            "determined. Ask the user to open and re-save the dashboard in Superset."
        )
    if GRID_ID not in tree.nodes:
        raise LayoutError(
            f"The dashboard layout has no {GRID_ID!r} node. Only standard grid dashboards "
            "can be rearranged by the copilot."
        )


# --- Traversal helpers ---------------------------------------------------


def _reachable(tree: LayoutTree) -> set[str]:
    """Node ids reachable from `ROOT_ID`.

    Superset keeps `HEADER_ID` outside the root's children, so unreachable
    nodes are normal and must not be treated as damage — only a *change* in
    reachability is.
    """
    seen: set[str] = set()
    stack = [ROOT_ID] if ROOT_ID in tree.nodes else []
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in tree.nodes:
            continue
        seen.add(node_id)
        stack.extend(tree.nodes[node_id].get("children", []))
    return seen


def _parent_of(tree: LayoutTree, node_id: str) -> str | None:
    """Immediate parent id, or `None` for roots and detached nodes."""
    for candidate_id, candidate in tree.nodes.items():
        if node_id in candidate.get("children", []):
            return candidate_id
    return None


def _ancestors(tree: LayoutTree, node_id: str) -> list[str]:
    """Ancestor ids from the outermost inwards, excluding the node itself."""
    chain: list[str] = []
    current = _parent_of(tree, node_id)
    guard = 0
    while current is not None and guard < len(tree.nodes) + 1:
        chain.append(current)
        current = _parent_of(tree, current)
        guard += 1
    chain.reverse()
    return chain


def _row_of(tree: LayoutTree, node_id: str) -> str:
    """The nearest enclosing ROW, or `LayoutError` if the node is not in one."""
    for ancestor in reversed(_ancestors(tree, node_id)):
        if tree.nodes[ancestor].get("type") == "ROW":
            return ancestor
    raise LayoutError(
        f"Layout node {node_id!r} is not inside a row, so row-level layout changes do "
        "not apply to it."
    )


def _container_of(tree: LayoutTree, node_id: str) -> str:
    """The nearest GRID or TAB ancestor — where new rows belong.

    Resolving to the enclosing tab rather than always to `GRID_ID` is what
    keeps "put the KPIs across the top" meaning *the top of the tab the user is
    looking at* on a tabbed dashboard.
    """
    for ancestor in reversed(_ancestors(tree, node_id)):
        if tree.nodes[ancestor].get("type") in CONTAINER_TYPES:
            return ancestor
    return GRID_ID


def _recompute_parents(tree: LayoutTree) -> None:
    """Rewrite every reachable node's `parents` from actual ancestry.

    Superset's frontend reads `parents` when deciding what a component is
    nested in, and a stale chain shows up as components rendering in the wrong
    tab. Recomputing after each operation is cheaper than trying to patch the
    chains that a move invalidated. Unreachable nodes are left alone so a
    standalone `HEADER_ID` keeps whatever it had.
    """

    def walk(node_id: str, chain: list[str]) -> None:
        node = tree.nodes.get(node_id)
        if node is None:
            return
        if node_id != ROOT_ID:
            node["parents"] = list(chain)
        for child in node.get("children", []):
            walk(child, [*chain, node_id])

    if ROOT_ID in tree.nodes:
        walk(ROOT_ID, [])


# --- Node construction ---------------------------------------------------


def _new_node_id(tree: LayoutTree, prefix: str, seed: str) -> str:
    """A fresh node id derived from `seed`, never colliding with an existing id.

    Superset uses a random short id here. We hash instead, because the module
    contract promises byte-identical output for identical input — a random id
    would break undo comparison and every snapshot diff. The hash is an
    identifier, not a security primitive.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    candidate = f"{prefix}-{digest}"
    suffix = 0
    while candidate in tree.nodes:
        suffix += 1
        candidate = f"{prefix}-{digest}{suffix}"
    return candidate


def _make_row(tree: LayoutTree, seed: str, children: list[str] | None = None) -> str:
    """Create and register an empty ROW node, returning its id."""
    row_id = _new_node_id(tree, "ROW", seed)
    tree.nodes[row_id] = {
        "id": row_id,
        "type": "ROW",
        "children": list(children or []),
        "parents": [],
        "meta": {"background": "BACKGROUND_TRANSPARENT"},
    }
    return row_id


def _meta(tree: LayoutTree, node_id: str) -> dict[str, Any]:
    """A node's `meta`, created if absent so callers can assign into it."""
    node = tree.node(node_id)
    meta = node.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        node["meta"] = meta
    return meta


def _width_of(tree: LayoutTree, node_id: str) -> int:
    """Declared width in grid columns, falling back to Superset's default."""
    value = _meta(tree, node_id).get("width")
    if isinstance(value, int) and value > 0:
        return min(value, GRID_COLUMN_COUNT)
    return GRID_DEFAULT_CHART_WIDTH


def _height_of(tree: LayoutTree, node_id: str) -> int:
    """Declared height in row units, falling back to Superset's default."""
    value = _meta(tree, node_id).get("height")
    if isinstance(value, int) and value > 0:
        return min(value, GRID_MAX_ROW_UNITS)
    return GRID_DEFAULT_CHART_HEIGHT


def _bounded(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    """Clamp an optional caller-supplied dimension into the grid's range."""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return max(minimum, min(value, maximum))


# --- Resolution ----------------------------------------------------------


def _resolve_chart(tree: LayoutTree, reference: Any) -> str:
    """Map a Superset slice id (or a raw node id) to exactly one CHART node id.

    A chart placed on the same dashboard twice is genuinely ambiguous — "make
    the encounters chart full width" cannot be honoured when there are two of
    them — so this raises rather than picking one and hoping.
    """
    if isinstance(reference, str) and reference in tree.nodes:
        if tree.nodes[reference].get("type") != "CHART":
            raise LayoutError(f"Layout node {reference!r} is not a chart.")
        return reference

    if isinstance(reference, bool) or not isinstance(reference, int):
        raise LayoutError(
            f"{reference!r} is not a Superset chart id. Use the numeric chart id from "
            "the dashboard's chart list."
        )

    matches = [
        node_id
        for node_id, node in tree.nodes.items()
        if node.get("type") == "CHART"
        and isinstance(node.get("meta"), dict)
        and node["meta"].get("chartId") == reference
    ]
    if not matches:
        present = tree.chart_ids()
        listed = ", ".join(str(cid) for cid in present) if present else "none"
        raise LayoutError(
            f"Chart {reference} is not on this dashboard, so it cannot be moved or "
            f"resized. Charts currently on it: {listed}."
        )
    if len(matches) > 1:
        raise LayoutError(
            f"Chart {reference} appears {len(matches)} times on this dashboard, so it is "
            "ambiguous which copy to change. Ask the user which one they mean."
        )
    return matches[0]


def _resolve_row(tree: LayoutTree, reference: Any) -> str:
    """Resolve a row directly by node id, or indirectly via a chart inside it.

    There are no stable row ids the model could have learnt from a read tool,
    which is why `equalize_row` identifies a row by one of its charts.
    """
    if isinstance(reference, str) and reference in tree.nodes:
        if tree.nodes[reference].get("type") == "ROW":
            return reference
        return _row_of(tree, reference)
    return _row_of(tree, _resolve_chart(tree, reference))


# --- Structural edits ----------------------------------------------------


def _detach(tree: LayoutTree, node_id: str) -> tuple[str, int]:
    """Unlink a node from its parent, returning that parent and the old index."""
    parent_id = _parent_of(tree, node_id)
    if parent_id is None:
        raise LayoutError(f"Layout node {node_id!r} has no parent and cannot be moved.")
    children = tree.nodes[parent_id]["children"]
    index = children.index(node_id)
    children.pop(index)
    return parent_id, index


def _prune_empty(tree: LayoutTree, node_id: str, removed: set[str]) -> None:
    """Delete a container left empty by a move, and its now-empty ancestors.

    Rows and columns exist only to hold things; leaving empties behind produces
    the mysterious blank bands users report after rearranging a dashboard.
    """
    current: str | None = node_id
    while current is not None:
        node = tree.nodes.get(current)
        if node is None:
            return
        if node.get("type") in UNPRUNABLE_TYPES or node.get("children"):
            return
        parent_id = _parent_of(tree, current)
        if parent_id is not None:
            tree.nodes[parent_id]["children"].remove(current)
        del tree.nodes[current]
        removed.add(current)
        current = parent_id


def _reflow_row(tree: LayoutTree, row_id: str) -> None:
    """Break a row that exceeds 12 columns into consecutive rows.

    Superset does not wrap: a row whose children sum past 12 renders squashed
    or clipped. Greedy line-breaking preserves left-to-right reading order,
    which is why "make this full width" pushes its neighbours down rather than
    shuffling them around it.

    Only rows an operation actually touched are reflowed. Reflowing everything
    would rewrite pre-existing layouts the user chose, and would break the
    no-op round-trip guarantee.
    """
    row = tree.nodes.get(row_id)
    if row is None or row.get("type") != "ROW":
        return

    groups: list[list[str]] = []
    current: list[str] = []
    used = 0
    for child_id in row["children"]:
        width = _width_of(tree, child_id)
        if current and used + width > GRID_COLUMN_COUNT:
            groups.append(current)
            current = []
            used = 0
        current.append(child_id)
        used += width
    if current:
        groups.append(current)
    if len(groups) <= 1:
        return

    row["children"] = groups[0]
    parent_id = _parent_of(tree, row_id)
    if parent_id is None:
        raise LayoutError(f"Row {row_id!r} has no parent container and cannot be reflowed.")
    parent_children = tree.nodes[parent_id]["children"]
    index = parent_children.index(row_id)
    for offset, group in enumerate(groups[1:], start=1):
        new_row_id = _make_row(tree, seed=f"{row_id}:reflow:{offset}", children=group)
        parent_children.insert(index + offset, new_row_id)


def _insert_row(
    tree: LayoutTree, container_id: str, seed: str, children: list[str], index: int
) -> str:
    """Create a row holding `children` and link it into `container_id`."""
    row_id = _make_row(tree, seed=seed, children=children)
    container_children = tree.nodes[container_id]["children"]
    position = max(0, min(index, len(container_children)))
    container_children.insert(position, row_id)
    return row_id


def _assert_width_editable(tree: LayoutTree, chart_node_id: str) -> None:
    """Refuse width changes on charts nested in a COLUMN.

    Inside a column the column's width governs the rendered size, so setting
    the chart's own width looks like it worked and changes nothing visible.
    Refusing with an explanation beats a silent no-op the model would then
    repeat.
    """
    parent_id = _parent_of(tree, chart_node_id)
    if parent_id is not None and tree.nodes[parent_id].get("type") == "COLUMN":
        raise LayoutError(
            "That chart sits inside a layout column, whose width governs how wide the "
            "chart renders. Resizing charts inside columns is not supported in this "
            "POC — move the chart out of the column first."
        )


# --- Operations ----------------------------------------------------------
#
# Each handler mutates the working copy in place and returns the ids it removed
# on purpose, which is what lets `apply_operation` tell a deliberate deletion
# apart from an accidental orphan.


def _op_set_width(tree: LayoutTree, operation: dict[str, Any], columns: int) -> set[str]:
    chart_node_id = _resolve_chart(tree, operation.get("chart_id"))
    _assert_width_editable(tree, chart_node_id)
    if not GRID_MIN_COLUMN_COUNT <= columns <= GRID_COLUMN_COUNT:
        raise LayoutError(
            f"A width of {columns} columns is outside the dashboard grid, which is "
            f"{GRID_COLUMN_COUNT} columns wide."
        )
    _meta(tree, chart_node_id)["width"] = columns
    _reflow_row(tree, _row_of(tree, chart_node_id))
    return set()


def _op_make_full_width(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    """Give the chart the whole 12-column width, pushing neighbours down."""
    return _op_set_width(tree, operation, GRID_COLUMN_COUNT)


def _op_make_half_width(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    return _op_set_width(tree, operation, GRID_COLUMN_COUNT // 2)


def _op_set_width_explicit(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    columns = operation.get("columns")
    if not isinstance(columns, int) or isinstance(columns, bool):
        raise LayoutError("set_width needs a whole number of grid columns between 1 and 12.")
    return _op_set_width(tree, operation, columns)


def _op_set_height(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    height = operation.get("height_units")
    if not isinstance(height, int) or isinstance(height, bool):
        raise LayoutError("set_height needs a whole number of row units between 1 and 100.")
    if not GRID_MIN_ROW_UNITS <= height <= GRID_MAX_ROW_UNITS:
        raise LayoutError(
            f"A height of {height} row units is out of range; Superset accepts "
            f"{GRID_MIN_ROW_UNITS}–{GRID_MAX_ROW_UNITS}."
        )
    chart_node_id = _resolve_chart(tree, operation.get("chart_id"))
    _meta(tree, chart_node_id)["height"] = height
    return set()


def _op_equalize_row(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    """Give every component in one row the same height and an even share of width.

    The row is identified by a chart inside it (or a row id, when a caller
    already has one). Remainder columns go to the leftmost components so the
    row fills exactly 12 and nothing is left ragged.
    """
    row_id = _resolve_row(tree, operation.get("chart_id", operation.get("row_id")))
    children = tree.nodes[row_id]["children"]
    if not children:
        raise LayoutError("That row is empty, so there is nothing to even out.")
    if len(children) > GRID_COLUMN_COUNT:
        raise LayoutError(
            f"That row holds {len(children)} components, which cannot each get at least "
            f"one of the {GRID_COLUMN_COUNT} grid columns. Split the row first."
        )

    base, remainder = divmod(GRID_COLUMN_COUNT, len(children))
    height = max(_height_of(tree, child) for child in children)
    for index, child_id in enumerate(children):
        meta = _meta(tree, child_id)
        meta["width"] = base + (1 if index < remainder else 0)
        meta["height"] = height
    return set()


def _op_move_relative(tree: LayoutTree, operation: dict[str, Any], *, before: bool) -> set[str]:
    """Place one chart immediately before or after another."""
    chart_node_id = _resolve_chart(tree, operation.get("chart_id"))
    target_node_id = _resolve_chart(tree, operation.get("target_chart_id"))
    if chart_node_id == target_node_id:
        raise LayoutError(
            "A chart cannot be moved relative to itself; name a different target chart."
        )

    removed: set[str] = set()
    source_parent_id, _ = _detach(tree, chart_node_id)
    target_parent_id = _parent_of(tree, target_node_id)
    if target_parent_id is None:
        raise LayoutError("The target chart is not inside a row, so nothing can be placed next to it.")

    target_children = tree.nodes[target_parent_id]["children"]
    insert_at = target_children.index(target_node_id) + (0 if before else 1)
    target_children.insert(insert_at, chart_node_id)

    if source_parent_id != target_parent_id:
        _prune_empty(tree, source_parent_id, removed)
    # Only rows overflow. A COLUMN stacks its children vertically, so widths
    # there never need line-breaking.
    if tree.nodes[target_parent_id].get("type") == "ROW":
        _reflow_row(tree, target_parent_id)
    return removed


def _op_move_after(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    return _op_move_relative(tree, operation, before=False)


def _op_move_before(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    return _op_move_relative(tree, operation, before=True)


def _op_move_to_edge(tree: LayoutTree, operation: dict[str, Any], *, to_top: bool) -> set[str]:
    """Move a chart into a row of its own at the top or bottom of its container."""
    chart_node_id = _resolve_chart(tree, operation.get("chart_id"))
    container_id = _container_of(tree, chart_node_id)
    container_children = tree.nodes[container_id]["children"]

    current_row_id = _row_of(tree, chart_node_id)
    already_alone = tree.nodes[current_row_id]["children"] == [chart_node_id]
    if already_alone and current_row_id in container_children:
        at_edge = container_children[0 if to_top else -1] == current_row_id
        if at_edge:
            return set()

    removed: set[str] = set()
    source_parent_id, _ = _detach(tree, chart_node_id)
    _prune_empty(tree, source_parent_id, removed)
    index = 0 if to_top else len(tree.nodes[container_id]["children"])
    _insert_row(
        tree,
        container_id,
        seed=f"{container_id}:{'top' if to_top else 'bottom'}:{chart_node_id}",
        children=[chart_node_id],
        index=index,
    )
    return removed


def _op_move_to_top(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    return _op_move_to_edge(tree, operation, to_top=True)


def _op_move_to_bottom(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    return _op_move_to_edge(tree, operation, to_top=False)


def _op_kpi_row(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    """Gather the named charts into one short row at the top of their container.

    Order follows `chart_ids` — the model has already decided the reading
    order, and re-sorting here would silently override it.
    """
    chart_ids = operation.get("chart_ids")
    if not isinstance(chart_ids, list) or not chart_ids:
        raise LayoutError("kpi_row needs a list of at least one chart id.")
    if len(chart_ids) > GRID_COLUMN_COUNT // 2:
        raise LayoutError(
            f"{len(chart_ids)} KPI tiles will not read on a {GRID_COLUMN_COUNT}-column "
            "grid; use at most 6."
        )

    node_ids = [_resolve_chart(tree, chart_id) for chart_id in chart_ids]
    if len(set(node_ids)) != len(node_ids):
        raise LayoutError("The same chart was listed more than once in the KPI row.")

    container_id = _container_of(tree, node_ids[0])

    removed: set[str] = set()
    for node_id in node_ids:
        source_parent_id, _ = _detach(tree, node_id)
        _prune_empty(tree, source_parent_id, removed)

    base, remainder = divmod(GRID_COLUMN_COUNT, len(node_ids))
    for index, node_id in enumerate(node_ids):
        meta = _meta(tree, node_id)
        meta["width"] = base + (1 if index < remainder else 0)
        meta["height"] = KPI_ROW_HEIGHT

    _insert_row(
        tree,
        container_id,
        seed=f"{container_id}:kpi:{','.join(node_ids)}",
        children=node_ids,
        index=0,
    )
    return removed


#: Stands in for a chart id that will not exist until the panel performs the
#: POST that creates it. `create_chart(dashboard_id=…)` needs a layout node for a
#: chart that does not exist yet, and the panel substitutes the real id after the
#: first call returns. Only `add_chart` accepts it, only when the caller opts in,
#: and it never reaches a comparison against real ids.
PLACEHOLDER_CHART_ID: Final[str] = "__NEW_ID__"


def _op_add_chart(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    """Place a newly saved chart on the dashboard.

    Lives here rather than in the write tool because adding a chart *is* a
    `position_json` mutation, and this module is the only code allowed to make
    one. `placement` mirrors the `add_chart_to_dashboard` schema.
    """
    chart_id = operation.get("chart_id")
    placeholder = chart_id == PLACEHOLDER_CHART_ID and operation.get("_allow_placeholder")
    if not placeholder:
        if isinstance(chart_id, bool) or not isinstance(chart_id, int):
            raise LayoutError("add_chart needs the numeric Superset chart id.")
        if chart_id in tree.chart_ids():
            raise LayoutError(f"Chart {chart_id} is already on this dashboard.")

    placement = operation.get("placement", "append")
    if placement not in {"append", "new_row", "kpi_row", "top"}:
        raise LayoutError(
            f"Unknown placement {placement!r}; use append, new_row, kpi_row or top."
        )

    is_kpi = placement == "kpi_row"
    width = _bounded(
        operation.get("width"), GRID_DEFAULT_CHART_WIDTH, GRID_MIN_COLUMN_COUNT, GRID_COLUMN_COUNT
    )
    height = _bounded(
        operation.get("height"),
        KPI_ROW_HEIGHT if is_kpi else GRID_DEFAULT_CHART_HEIGHT,
        GRID_MIN_ROW_UNITS,
        GRID_MAX_ROW_UNITS,
    )
    # Node ids are hashed from a seed rather than drawn at random, so the module
    # can promise byte-identical output for identical input. With a placeholder
    # chart id that seed is the same every time, and Superset keys chart
    # membership on the node id — two identically keyed nodes would silently
    # detach a chart. Salting with the layout's existing chart ids keeps the id
    # unique per dashboard while staying deterministic.
    seed = f"chart:{chart_id}"
    if placeholder:
        seed += ":new:" + ",".join(str(cid) for cid in tree.chart_ids())
    node_id = _new_node_id(tree, "CHART", seed)
    tree.nodes[node_id] = {
        "id": node_id,
        "type": "CHART",
        "children": [],
        "parents": [],
        "meta": {
            "chartId": chart_id,
            "width": width,
            "height": height,
            **({"sliceName": operation["slice_name"]} if operation.get("slice_name") else {}),
            **({"uuid": operation["chart_uuid"]} if operation.get("chart_uuid") else {}),
        },
    }

    grid_children = tree.nodes[GRID_ID]["children"]
    rows = [child for child in grid_children if tree.nodes[child].get("type") == "ROW"]

    if placement == "append" and rows:
        last_row_id = rows[-1]
        tree.nodes[last_row_id]["children"].append(node_id)
        _reflow_row(tree, last_row_id)
    elif is_kpi and rows:
        first_row_id = rows[0]
        tree.nodes[first_row_id]["children"].append(node_id)
        _op_equalize_row(tree, {"row_id": first_row_id})
        _reflow_row(tree, first_row_id)
    else:
        index = 0 if placement in {"top", "kpi_row"} else len(grid_children)
        _insert_row(
            tree, GRID_ID, seed=f"add:{chart_id}:{placement}", children=[node_id], index=index
        )
    return set()


def _op_remove_chart(tree: LayoutTree, operation: dict[str, Any]) -> set[str]:
    """Take a chart off the dashboard, tidying away any row it emptied."""
    chart_node_id = _resolve_chart(tree, operation.get("chart_id"))
    parent_id, _ = _detach(tree, chart_node_id)
    del tree.nodes[chart_node_id]
    removed = {chart_node_id}
    _prune_empty(tree, parent_id, removed)
    return removed


_OPERATIONS: Final[dict[str, Any]] = {
    "make_full_width": _op_make_full_width,
    "make_half_width": _op_make_half_width,
    "set_width": _op_set_width_explicit,
    "set_height": _op_set_height,
    "equalize_row": _op_equalize_row,
    "move_after": _op_move_after,
    "move_before": _op_move_before,
    "move_to_top": _op_move_to_top,
    "move_to_bottom": _op_move_to_bottom,
    "kpi_row": _op_kpi_row,
    "add_chart": _op_add_chart,
    "remove_chart": _op_remove_chart,
}

#: The vocabulary the model may use. Kept in step with the `operation` consts in
#: `validation/schemas/actions.py`, plus `add_chart`/`remove_chart`, which the
#: dashboard-membership actions compile down to.
SUPPORTED_OPERATIONS: Final[frozenset[str]] = frozenset(_OPERATIONS)


# --- Public entry points -------------------------------------------------


def apply_operation(tree: LayoutTree, operation: dict[str, Any]) -> LayoutTree:
    """Apply one semantic operation, returning a new tree.

    The input tree is untouched whether the operation succeeds or fails, so a
    caller applying a batch can abandon the whole batch on the first
    `LayoutError` and still hold a valid layout.
    """
    if not isinstance(operation, dict):
        raise LayoutError(
            f"A layout operation must be an object, got {type(operation).__name__}."
        )
    name = operation.get("operation")
    handler = _OPERATIONS.get(name) if isinstance(name, str) else None
    if handler is None:
        supported = ", ".join(sorted(SUPPORTED_OPERATIONS))
        raise LayoutError(
            f"Unknown layout operation {name!r}. Supported operations are: {supported}."
        )

    working = tree.copy()
    before = _reachable(tree)
    removed = handler(working, operation)
    _recompute_parents(working)

    after = _reachable(working)
    lost = (before - after) - removed
    if lost:
        raise LayoutError(
            f"Operation {name!r} would detach {', '.join(sorted(lost))} from the "
            "dashboard, so it was not applied."
        )
    stranded = (set(working.nodes) - after) - (set(tree.nodes) - before)
    if stranded:
        raise LayoutError(
            f"Operation {name!r} would leave {', '.join(sorted(stranded))} unattached to "
            "the dashboard, so it was not applied."
        )
    return working


def compile_operations(
    raw: str | dict[str, Any] | None,
    operations: list[dict[str, Any]],
    *,
    allow_placeholder_chart_id: bool = False,
) -> str:
    """Parse, apply every operation in order, and serialise.

    The end-to-end call a write tool makes. An empty `operations` list is a
    legitimate no-op that normalises the layout — and on a freshly created
    dashboard, where `raw` is `None`, it is how the first valid skeleton gets
    written, because `parse_position_json(None)` returns `empty_skeleton()`.

    `allow_placeholder_chart_id` opts one caller into `add_chart` accepting
    `PLACEHOLDER_CHART_ID`: `create_chart(dashboard_id=…)` must lay out a chart
    whose id will not exist until the browser performs the POST. It is off by
    default so no ordinary layout edit can smuggle a non-numeric chart id into a
    saved dashboard.
    """
    tree = parse_position_json(raw)
    for operation in operations:
        if allow_placeholder_chart_id:
            operation = {**operation, "_allow_placeholder": True}
        tree = apply_operation(tree, operation)
    return serialise(tree)
