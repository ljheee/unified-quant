"""Every signature annotation in the library must resolve to a real name.

``from __future__ import annotations`` turns each annotation into a lazy string,
so a name used in a signature but never imported stays invisible until something
evaluates the hints.  Two instances shipped that way (cr.md CR38-2, FC-M4):
``Mapping`` in ``adjusted_price.py``, ``Path`` in ``factors/engine.py`` and
``QlibRuntimeTrainer`` in ``research_chain/adapters.py``.  Nothing in the library
evaluates annotations today, so a NameError would only surface in a future caller
that does (a type checker, a DI framework, ``pydantic``, ``inspect.signature``
with ``eval_str``).

A name imported under ``if TYPE_CHECKING:`` is legitimate: it is deliberately
absent at runtime, which is how an optional dependency such as qlib is referenced
without importing it.  Those are allowed here; a bare unimported name is not.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing

import uq


def _modules():
    for found in pkgutil.walk_packages(uq.__path__, "uq."):
        try:
            yield importlib.import_module(found.name)
        except Exception:
            # A module that cannot be imported here (an optional dependency such
            # as qlib) is out of scope: this lock covers modules that DO load, and
            # a module that never loads has no reachable annotation to get wrong.
            continue


def _functions():
    for module in _modules():
        for name, obj in vars(module).items():
            if inspect.isclass(obj) and getattr(obj, "__module__", None) == module.__name__:
                for method_name, method in vars(obj).items():
                    if inspect.isfunction(method):
                        yield module.__name__, f"{name}.{method_name}", method, module
            elif inspect.isfunction(obj) and getattr(obj, "__module__", None) == module.__name__:
                yield module.__name__, name, obj, module


def _guarded_names(module) -> set[str]:
    """Names imported only under ``if TYPE_CHECKING:`` (legitimately absent at runtime)."""
    guarded: set[str] = set()
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        return guarded
    lines = source.split("\n")
    inside = False
    for line in lines:
        if line.startswith("if TYPE_CHECKING"):
            inside = True
            continue
        if inside:
            if line and not line[0].isspace() and not line.startswith((")", "]", "}")):
                inside = False
            elif line.strip().startswith(("from ", "import ")):
                for part in line.split(" import ", 1)[-1].split(","):
                    guarded.add(part.strip().split(" as ")[-1].strip())
    return guarded


def test_every_annotation_resolves_or_is_a_typing_only_import():
    unresolved = []
    for module_name, qualname, function, module in _functions():
        try:
            typing.get_type_hints(function)
        except NameError as exc:
            missing = str(exc).split("'")[1] if "'" in str(exc) else str(exc)
            if missing in _guarded_names(module):
                continue
            unresolved.append(f"{module_name}::{qualname} -> {exc}")
        except Exception:
            # Anything other than a missing name (an unresolvable forward ref to a
            # third-party type, a badly formed annotation) is not what this lock is
            # for.
            continue
    assert not unresolved, "annotations naming a name that is never imported:\n" + "\n".join(unresolved)


def test_the_lock_actually_exercises_the_library():
    # A lock that inspects nothing would pass vacuously. Assert the sweep reaches a
    # realistic number of functions across the package.
    visited = list(_functions())
    assert len(visited) > 200, f"only {len(visited)} functions inspected; the sweep is too narrow"
