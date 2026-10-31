"""Registry of named, parametric, seeded SVG generators.

A generator is a plain function ``fn(gid, style, [rng,] **params) -> str`` returning an SVG
fragment: a ``<g>`` whose descendants all carry stable ids and ``inkscape:label``s, so the
output stays editable like hand-drawn work. Registering it derives the parameter schema from
the signature (types, defaults, required) plus optional ranges, choices and docs, which gives
every generator the same validation, the same catalogue entry and the same seeding:
``build(name, params, style, seed)`` is deterministic for a given seed.
"""

from __future__ import annotations

import importlib
import inspect
import random
import typing
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

# Modules whose import registers generators. Imported lazily (see _load_all) so that a
# generator module can import this registry without an import cycle.
_GENERATOR_MODULES = (
    "inkscape_mcp.figure_kit",
    "inkscape_mcp.generators.scatter",
    "inkscape_mcp.generators.organic",
    "inkscape_mcp.generators.flow",
    "inkscape_mcp.generators.tiling",
)

_RESERVED = {"gid", "style", "rng"}


@dataclass
class Param:
    name: str
    kind: str  # float | int | bool | str | choice
    default: Any = None
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    doc: str = ""


@dataclass
class Generator:
    name: str
    category: str
    doc: str
    fn: Callable[..., str] = field(repr=False)
    params: dict[str, Param]
    seeded: bool

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "doc": self.doc,
            "seeded": self.seeded,
            "params": [{k: v for k, v in asdict(p).items() if v not in (None, "", False) or k == "required"}
                       for p in self.params.values()],
        }  # fmt: skip


REGISTRY: dict[str, Generator] = {}


def _kind_of(annotation: Any) -> str:
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    for kind in ("bool", "int", "float", "str"):
        if text == kind or text.startswith(f"{kind} |") or text.endswith(f"| {kind}"):
            return kind
    return "str"


def register(
    fn: Callable[..., str],
    category: str,
    *,
    name: str | None = None,
    ranges: dict[str, tuple[float, float]] | None = None,
    choices: dict[str, list[str]] | None = None,
    docs: dict[str, str] | None = None,
) -> Callable[..., str]:
    """Register `fn`; its parameter schema is read from its signature."""
    ranges, choices, docs = ranges or {}, choices or {}, docs or {}
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # unresolvable forward refs: fall back to the raw annotations
        hints = {}
    params: dict[str, Param] = {}
    for pname, p in inspect.signature(fn).parameters.items():
        if pname in _RESERVED:
            continue
        annotation = hints.get(pname, p.annotation)
        kind = (
            "choice"
            if pname in choices
            else _kind_of(annotation if annotation is not inspect.Parameter.empty else "str")
        )
        lo, hi = ranges.get(pname, (None, None))
        params[pname] = Param(
            name=pname,
            kind=kind,
            default=None if p.default is inspect.Parameter.empty else p.default,
            required=p.default is inspect.Parameter.empty,
            minimum=lo,
            maximum=hi,
            choices=choices.get(pname),
            doc=docs.get(pname, ""),
        )
    gen_name = name or fn.__name__
    doc = inspect.cleandoc(fn.__doc__ or "").split("\n\n")[0]
    REGISTRY[gen_name] = Generator(gen_name, category, doc, fn, params, "rng" in inspect.signature(fn).parameters)
    return fn


def generator(category: str, **kwargs: Any) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorator form of `register`."""

    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        return register(fn, category, **kwargs)

    return wrap


def _load_all() -> None:
    for module in _GENERATOR_MODULES:
        importlib.import_module(module)


def _coerce(gen: str, p: Param, value: Any) -> Any:
    if value is None and not p.required:
        return None
    try:
        if p.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError
        elif p.kind == "int":
            if isinstance(value, bool) or float(value) != int(value):
                raise ValueError
            value = int(value)
        elif p.kind == "float":
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
        else:
            value = str(value)
    except (TypeError, ValueError):
        raise ValueError(f"{gen}.{p.name} must be {p.kind} (got {value!r})") from None
    if p.choices is not None and value not in p.choices:
        raise ValueError(f"{gen}.{p.name} must be one of {p.choices} (got {value!r})")
    if (p.minimum is not None and value < p.minimum) or (p.maximum is not None and value > p.maximum):
        raise ValueError(f"{gen}.{p.name} must be within [{p.minimum}, {p.maximum}] (got {value!r})")
    return value


def get(name: str) -> Generator:
    _load_all()
    if name not in REGISTRY:
        raise ValueError(f"unknown generator {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name]


def build(name: str, params: dict[str, Any], style: dict[str, Any], seed: int | None = None) -> str:
    """Validate `params` (must include "id") and run generator `name`. Deterministic per seed."""
    gen = get(name)
    params = dict(params)
    gid = params.pop("id", None)
    if not gid or not isinstance(gid, str):
        raise ValueError("params.id is required (generated parts need stable ids)")
    unknown = sorted(set(params) - set(gen.params))
    if unknown:
        raise ValueError(f"{name}: unknown param(s) {unknown}; valid: {sorted(gen.params)}")
    kwargs: dict[str, Any] = {}
    for p in gen.params.values():
        if p.name in params:
            kwargs[p.name] = _coerce(name, p, params[p.name])
        elif p.required:
            raise ValueError(f"{name}: missing required param {p.name!r}")
    if gen.seeded:
        kwargs["rng"] = random.Random(0 if seed is None else seed)  # noqa: S311 — seeded art, not crypto
    return gen.fn(gid, style=style, **kwargs)


def catalogue(category: str | None = None) -> list[dict[str, Any]]:
    _load_all()
    return [g.describe() for g in sorted(REGISTRY.values(), key=lambda g: (g.category, g.name))
            if category in (None, g.category)]  # fmt: skip
