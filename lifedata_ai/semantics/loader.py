"""Load and query the semantics registry (spec §8).

The same loaded object feeds three consumers, which is the whole point: what the
model is *told*, what the validator *permits*, and what is *projected onto
Superset* all come from one file and cannot drift apart.

    registry.yaml
        │
        ├── agent/prompts.py       → the catalogue in the prompt's stable prefix
        ├── validation/validator.py→ the groupby allowlist and the viz allowlist
        └── management/commands/register_superset_datasets.py --sync-metadata
                                   → Superset saved metrics, verbose_name, d3format

Column `role` is the load-bearing addition. It is an **allowlist**: only columns
declared `dimension` or `temporal` may be grouped by. That is chosen over
forbidding `key` specifically for three reasons — it is one rule rather than a
growing list of forbidden roles; it matches the catalogue's own claim to be
complete; and it closes the case a denylist leaves open, where a column the
registry never declared at all (a new view column, a future dataset) silently
becomes groupable.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REGISTRY_PATH = Path(__file__).resolve().parent / "registry.yaml"

#: Column roles that may appear as a `groupby` entry or as `x_axis`. On a bar or
#: timeseries chart the axis *is* a grouping, so both go through the same gate.
GROUPABLE_ROLES: frozenset[str] = frozenset({"dimension", "temporal"})

#: Every role the registry may declare. An unknown role is a typo, and a typo
#: that silently means "not groupable" would be a confusing refusal; one that
#: silently meant "groupable" would be a hole. Neither: the loader raises.
KNOWN_ROLES: frozenset[str] = frozenset(
    {"dimension", "temporal", "measure_source", "key", "internal", "map_key", "geometry"}
)


class RegistryError(ValueError):
    """registry.yaml is malformed. Raised at load time, not at request time."""


@dataclass(frozen=True)
class Column:
    """One column of a reporting view, as the registry describes it."""

    name: str
    role: str
    label: str = ""
    #: A plain-English hint shown to the user when this column is offered as a
    #: choice — "HC II / HC III / HC IV / Hospital". This is how a non-technical
    #: executive picks between four columns without learning what a column is.
    note: str = ""

    @property
    def is_groupable(self) -> bool:
        return self.role in GROUPABLE_ROLES

    @property
    def display_label(self) -> str:
        """Never show a bare column name to a user without a label beside it."""
        return self.label or self.name.replace("_", " ").capitalize()


@dataclass(frozen=True)
class Metric:
    """A saved metric this registry projects onto its Superset dataset.

    `expression` is SQL, written by a human in a reviewed file and applied by an
    administrator — never composed at request time and never seen by the model.
    See the header of registry.yaml for why that is the rule rather than an
    exception to it.
    """

    name: str
    dataset: str
    expression: str
    label: str = ""
    #: A d3 format string (",d", ".1%", ".1f"). Projected onto the Superset saved
    #: metric so every chart using it formats correctly with no model knowledge
    #: of d3 syntax at all.
    d3format: str = ""
    #: Back-link to a governed indicator, so `explain_indicator` and the chart
    #: drawn on screen describe the same calculation.
    indicator: str = ""

    @property
    def display_label(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


@dataclass(frozen=True)
class Dataset:
    name: str
    superset_dataset_id: int
    grain: str
    subject_areas: list[str]
    sensitivity: str
    label: str = ""
    one_row_is: str = ""
    synonyms: list[str] = field(default_factory=list)
    geography: str | None = None
    status_field: str | None = None
    form_key: str | None = None
    small_cell_threshold: int | None = None
    time_column: str | None = None
    #: Which column an unqualified "by facility" means. Declaring it makes the
    #: common case deterministic and free — the model uses it and says that it
    #: did, instead of asking a question with an obvious answer.
    default_dimension: str | None = None
    default_time_range: str = ""
    notes: str = ""
    traps: str = ""
    columns: tuple[Column, ...] = ()
    metrics: tuple[Metric, ...] = ()

    @property
    def display_label(self) -> str:
        return self.label or self.name

    @property
    def is_patient_geography(self) -> bool:
        """Whether the district-floor / no-point-map policy applies (spec §6.4)."""
        return self.geography == "patient"

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def dimensions(self) -> list[Column]:
        return [c for c in self.columns if c.role == "dimension"]

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)


@dataclass(frozen=True)
class Indicator:
    name: str
    dataset: str
    definition: str
    unit: str
    counts: str
    expression: str = ""
    numerator: str = ""
    denominator: str = ""
    caveats: str = ""


@dataclass(frozen=True)
class Registry:
    datasets: dict[str, Dataset]
    indicators: dict[str, Indicator]
    viz_allowlist: list[str]
    patient_geography_forbidden_viz: list[str]
    color_schemes: list[str] = field(default_factory=list)
    layout_presets: list[dict[str, Any]] = field(default_factory=list)

    # -- lookups ----------------------------------------------------------

    def dataset(self, name: str) -> Dataset | None:
        return self.datasets.get(name)

    def dataset_by_superset_id(self, dataset_id: int) -> Dataset | None:
        return next(
            (d for d in self.datasets.values() if d.superset_dataset_id == dataset_id), None
        )

    def indicator(self, name: str) -> Indicator | None:
        return self.indicators.get(name)

    def is_viz_allowed(self, viz_type: str) -> bool:
        return viz_type in self.viz_allowlist

    def metric(self, name: str, dataset: str | None = None) -> Metric | None:
        """A saved metric by name, optionally scoped to one dataset.

        Metric names are unique across the registry (the loader enforces it), so
        the unscoped lookup is unambiguous — which is what lets the receipt card
        turn `malaria_positivity` into "Malaria positivity" without knowing which
        dataset a chart is on.
        """
        for ds in self.datasets.values():
            if dataset is not None and ds.name != dataset:
                continue
            found = ds.metric(name)
            if found is not None:
                return found
        return None

    def column(self, name: str, dataset: str | None = None) -> Column | None:
        """A column by name. Unscoped, the first declaration wins.

        Column names repeat across datasets on purpose — `facility_name` means
        the same thing everywhere — so an unscoped lookup returning any of them
        gives the right *label*. Anything policing a column (the groupby
        allowlist) must scope to the dataset; see `is_groupable`.
        """
        for ds in self.datasets.values():
            if dataset is not None and ds.name != dataset:
                continue
            found = ds.column(name)
            if found is not None:
                return found
        return None

    def rank_datasets(self, goal: str) -> list[tuple[Dataset, str]]:
        """Rank datasets against an analysis goal by declared vocabulary.

        Kept deliberately simple and kept honest about what it is. The registry
        now declares `synonyms`, so this matches far more than it did — the
        original scored whole-token equality against `subject_areas` only, which
        meant "lab orders by facility" scored zero against
        `laboratory/turnaround/testing` and `recommend_dataset` then dumped all
        five datasets unranked.

        It is still not a phrase resolver and must not grow into one. Ambiguity
        is answered by *asking* — see `ask_user` — because a resolver's misses
        produce confident wrong answers with no signal, while a question produces
        the right chart.
        """
        text = goal.lower()
        words = {w.strip(".,?!:;\"'()").lower() for w in text.split()}
        scored: list[tuple[int, Dataset, str]] = []
        for ds in self.datasets.values():
            hits: list[str] = []
            score = 0
            for area in ds.subject_areas:
                if area.lower() in words:
                    score += 2
                    hits.append(area)
            for synonym in ds.synonyms:
                # Multi-word synonyms ("where do patients come from") are matched
                # as phrases; single words against the token set, so "labs" does
                # not match inside "syllabus".
                matched = synonym in text if " " in synonym else synonym.lower() in words
                if matched:
                    score += 2
                    hits.append(synonym)
            if ds.name.lower() in text:
                score += 5
                hits.append(ds.name)
            if score:
                why = f"matches {', '.join(dict.fromkeys(hits))}"
                scored.append((score, ds, why))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [(ds, why) for _score, ds, why in scored]

    def excerpt_for(self, dataset_names: list[str]) -> dict[str, Any]:
        """A prompt-sized slice of the registry.

        Spec §5 is explicit that the system prompt gets a *relevant excerpt*,
        not the whole file, to control token usage.
        """
        chosen = [self.datasets[n] for n in dataset_names if n in self.datasets]
        relevant_indicators = [
            i for i in self.indicators.values() if i.dataset in {d.name for d in chosen}
        ]
        return {
            "datasets": [
                {
                    "name": d.name,
                    "grain": d.grain,
                    "geography": d.geography,
                    "sensitivity": d.sensitivity,
                    "notes": d.notes,
                }
                for d in chosen
            ],
            "indicators": [
                {
                    "name": i.name,
                    "definition": i.definition,
                    "unit": i.unit,
                    "counts": i.counts,
                    "caveats": i.caveats,
                }
                for i in relevant_indicators
            ],
        }


def _build_columns(dataset_name: str, raw: list[dict[str, Any]]) -> tuple[Column, ...]:
    columns: list[Column] = []
    seen: set[str] = set()
    for entry in raw:
        name = entry.get("name")
        if not name:
            raise RegistryError(f"{dataset_name}: a column entry has no name.")
        if name in seen:
            raise RegistryError(f"{dataset_name}: column {name!r} is declared twice.")
        seen.add(name)
        role = entry.get("role")
        if role not in KNOWN_ROLES:
            raise RegistryError(
                f"{dataset_name}.{name}: role {role!r} is not one of "
                f"{sorted(KNOWN_ROLES)}. An unknown role would silently change "
                "whether the column can be grouped by."
            )
        columns.append(
            Column(
                name=name,
                role=role,
                label=(entry.get("label") or "").strip(),
                note=" ".join((entry.get("note") or "").split()),
            )
        )
    return tuple(columns)


def _build_metrics(dataset_name: str, raw: list[dict[str, Any]]) -> tuple[Metric, ...]:
    metrics: list[Metric] = []
    for entry in raw:
        name = entry.get("name")
        expression = " ".join((entry.get("expression") or "").split())
        if not name or not expression:
            raise RegistryError(
                f"{dataset_name}: a metric entry needs both a name and an expression."
            )
        metrics.append(
            Metric(
                name=name,
                dataset=dataset_name,
                expression=expression,
                label=(entry.get("label") or "").strip(),
                d3format=(entry.get("d3format") or "").strip(),
                indicator=(entry.get("indicator") or "").strip(),
            )
        )
    return tuple(metrics)


@functools.lru_cache(maxsize=1)
def load_registry() -> Registry:
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))

    datasets: dict[str, Dataset] = {}
    for d in raw.get("datasets", []):
        name = d["name"]
        datasets[name] = Dataset(
            name=name,
            superset_dataset_id=d["superset_dataset_id"],
            grain=d["grain"],
            subject_areas=d.get("subject_areas", []),
            sensitivity=d.get("sensitivity", "aggregate_ok"),
            label=(d.get("label") or "").strip(),
            one_row_is=" ".join((d.get("one_row_is") or "").split()),
            synonyms=[str(s).lower() for s in d.get("synonyms", [])],
            geography=d.get("geography"),
            status_field=d.get("status_field"),
            form_key=d.get("form_key"),
            small_cell_threshold=d.get("small_cell_threshold"),
            time_column=d.get("time_column"),
            default_dimension=d.get("default_dimension"),
            default_time_range=(d.get("default_time_range") or "").strip(),
            notes=" ".join((d.get("notes") or "").split()),
            traps=" ".join((d.get("traps") or "").split()),
            columns=_build_columns(name, d.get("columns") or []),
            metrics=_build_metrics(name, d.get("metrics") or []),
        )

    _check_cross_references(datasets, raw)

    indicators = {
        i["name"]: Indicator(
            name=i["name"],
            dataset=i["dataset"],
            definition=" ".join((i.get("definition") or "").split()),
            unit=i.get("unit", ""),
            counts=i.get("counts", ""),
            expression=i.get("expression", ""),
            numerator=i.get("numerator", ""),
            denominator=i.get("denominator", ""),
            caveats=" ".join((i.get("caveats") or "").split()),
        )
        for i in raw.get("indicators", [])
    }

    for dataset in datasets.values():
        for metric in dataset.metrics:
            if metric.indicator and metric.indicator not in indicators:
                raise RegistryError(
                    f"{dataset.name}.{metric.name} back-links to indicator "
                    f"{metric.indicator!r}, which is not defined. The link is what "
                    "keeps the quoted definition and the computed chart in step."
                )

    return Registry(
        datasets=datasets,
        indicators=indicators,
        viz_allowlist=raw.get("viz_allowlist", []),
        patient_geography_forbidden_viz=raw.get("patient_geography_forbidden_viz", []),
        color_schemes=raw.get("color_schemes", []),
        layout_presets=raw.get("layout_presets", []),
    )


def _check_cross_references(datasets: dict[str, Dataset], raw: dict[str, Any]) -> None:
    """Fail at load time on a registry that would mislead at request time.

    Every one of these is a mistake that would otherwise surface as a baffling
    chart rather than as an error: a `default_dimension` naming a column that
    does not exist means "by facility" silently produces nothing; a duplicate
    metric name means `registry.metric()` returns whichever came first.
    """
    metric_owners: dict[str, str] = {}
    for dataset in datasets.values():
        if dataset.default_dimension:
            column = dataset.column(dataset.default_dimension)
            if column is None:
                raise RegistryError(
                    f"{dataset.name}: default_dimension {dataset.default_dimension!r} "
                    "is not a declared column."
                )
            if not column.is_groupable:
                raise RegistryError(
                    f"{dataset.name}: default_dimension {column.name!r} has role "
                    f"{column.role!r} and so may not be grouped by."
                )
        if dataset.time_column and dataset.column(dataset.time_column) is None:
            raise RegistryError(
                f"{dataset.name}: time_column {dataset.time_column!r} is not a "
                "declared column."
            )
        for metric in dataset.metrics:
            if metric.name in metric_owners:
                raise RegistryError(
                    f"Metric {metric.name!r} is declared on both "
                    f"{metric_owners[metric.name]} and {dataset.name}. Metric names "
                    "must be unique so an unscoped lookup is unambiguous."
                )
            metric_owners[metric.name] = dataset.name

    declared_ids = [d.superset_dataset_id for d in datasets.values()]
    if len(set(declared_ids)) != len(declared_ids):
        raise RegistryError(
            "Two datasets claim the same superset_dataset_id; the geography policy "
            "would then be applied against whichever the lookup happened to find."
        )
