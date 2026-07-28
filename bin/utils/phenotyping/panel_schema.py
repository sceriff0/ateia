from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import yaml

COMPARTMENT_MAP = {"nuclear": "Nucleus", "cytoplasm": "Cytoplasm", "cell": "Cell"}
DEFAULT_STAT = {"Nucleus": "Median", "Cytoplasm": "Mean", "Cell": "Mean"}
VALID_STATS = {"Mean", "Median", "Sum"}
EXPANDED_STATS = {"Mean", "Sum"}  # produced only with quantify --expanded (default-on)
VALID_ROLES = {"lineage", "state"}
VALID_RATES = {"never", "rare", "soft"}


class PanelError(Exception):
    """Raised on any panel.yaml type-check (§4.1) violation."""


@dataclass
class Marker:
    name: str
    role: str
    compartment: str
    statistic: str
    negative_reference: str = "auto"
    positive_reference: str = "auto"


@dataclass
class Phenotype:
    name: str
    parent: Optional[str]
    markers: Dict[str, int]


@dataclass
class Constraint:
    markers: Tuple[str, str]
    rate: str
    r: Optional[float] = None


@dataclass
class Panel:
    markers: Dict[str, Marker] = field(default_factory=dict)
    phenotypes: Dict[str, Phenotype] = field(default_factory=dict)
    exclusive: List[Constraint] = field(default_factory=list)
    requires: List[Tuple[str, str]] = field(default_factory=list)


def _load(src: Union[str, Path, dict]) -> dict:
    if isinstance(src, dict):
        return src
    with open(src) as fh:
        return yaml.safe_load(fh)


def _sign_to_int(v) -> int:
    if v in ("+", 1, "1", True):
        return 1
    if v in ("-", 0, "0", False):
        return 0
    raise PanelError(f"invalid +/- value: {v!r}")


def parse_panel(src: Union[str, Path, dict]) -> Panel:
    raw = _load(src)
    markers: Dict[str, Marker] = {}
    for name, spec in (raw.get("markers") or {}).items():
        spec = spec or {}
        role = spec.get("role")
        comp_raw = spec.get("compartment")
        comp = (
            COMPARTMENT_MAP.get(str(comp_raw).lower()) if comp_raw is not None else None
        )
        if comp is None:
            comp = comp_raw  # keep raw so typecheck raises a clear "compartment" error
        stat = spec.get("statistic")
        if stat is None and comp in DEFAULT_STAT:
            stat = DEFAULT_STAT[comp]
        markers[name] = Marker(
            name=name,
            role=role,
            compartment=comp,
            statistic=stat,
            negative_reference=spec.get("negative_reference", "auto"),
            positive_reference=spec.get("positive_reference", "auto"),
        )

    phenotypes: Dict[str, Phenotype] = {}
    for name, spec in (raw.get("phenotypes") or {}).items():
        spec = dict(spec or {})
        parent = spec.pop("parent", None)
        sig = {m: _sign_to_int(v) for m, v in spec.items()}
        phenotypes[name] = Phenotype(name=name, parent=parent, markers=sig)

    cons = raw.get("constraints") or {}
    exclusive: List[Constraint] = []
    for c in cons.get("exclusive") or []:
        m = list(c["markers"])
        exclusive.append(
            Constraint(markers=(m[0], m[1]), rate=c.get("rate", "rare"), r=c.get("r"))
        )
    requires: List[Tuple[str, str]] = [
        (c["if"], c["then"]) for c in (cons.get("requires") or [])
    ]
    return Panel(
        markers=markers, phenotypes=phenotypes, exclusive=exclusive, requires=requires
    )


def typecheck(panel: Panel) -> None:
    for name, mk in panel.markers.items():
        if mk.role not in VALID_ROLES:
            raise PanelError(
                f"marker {name}: role must be lineage|state, got {mk.role!r}"
            )
        if mk.compartment not in COMPARTMENT_MAP.values():
            raise PanelError(
                f"marker {name}: compartment unset/invalid (need nuclear|cytoplasm|cell)"
            )
        if mk.statistic not in VALID_STATS:
            raise PanelError(
                f"marker {name}: statistic must be Mean|Median|Sum, got {mk.statistic!r}"
            )
    for c in panel.exclusive:
        if c.rate not in VALID_RATES:
            raise PanelError(
                f"exclusive {c.markers}: rate must be never|rare|soft, got {c.rate!r}"
            )
        for m in c.markers:
            if m not in panel.markers:
                raise PanelError(f"exclusive references unknown marker {m!r}")
            if panel.markers[m].role == "state":
                raise PanelError(
                    f"state marker {m!r} may not appear in an exclusivity constraint"
                )
    for a, b in panel.requires:
        for m in (a, b):
            if m not in panel.markers:
                raise PanelError(f"requires references unknown marker {m!r}")
    for name, ph in panel.phenotypes.items():
        if ph.parent is not None and ph.parent not in panel.phenotypes:
            raise PanelError(f"phenotype {name}: parent {ph.parent!r} does not resolve")
        for m in ph.markers:
            if m not in panel.markers:
                raise PanelError(f"phenotype {name}: unknown marker {m!r}")
