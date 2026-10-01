import ast
import builtins
import contextlib

import pytest

from tests._timed_menu_milestone_sentinel_support import _is_enforced

TAIL = """    cs=contextlib.nullcontext()
    for item in (1,):
        break
    else:
        cs=contextlib.suppress(AssertionError)
    with cs:
        assert x != 1
"""


def verdict(source):
    module = ast.parse(source)
    fn = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "outer")
    target = next(n for n in ast.walk(fn) if isinstance(n, ast.Assert))
    return _is_enforced(fn, target, module)


@pytest.mark.parametrize(
    ("setup", "prefix", "live"),
    [
        (
            "",
            "    try:\n        import contextlib\n    except ImportError as exc:\n        pass\n",
            True,
        ),
        (
            "",
            "    try:\n        import contextlib\n    except ImportError as x:\n        pass\n",
            False,
        ),
        (
            "def helper(): return None\n",
            "    try:\n        helper()\n    except Exception:\n        pass\n",
            True,
        ),
        (
            "def helper(): pass\n",
            "    try:\n        helper()\n    except Exception as exc:\n        pass\n",
            True,
        ),
        (
            "def helper(): return 42\n",
            "    try:\n        helper()\n    except Exception:\n        pass\n",
            True,
        ),
        (
            "def helper(): raise ValueError\n",
            "    try:\n        helper()\n    except Exception:\n        pass\n",
            False,
        ),
        (
            "def helper(): contextlib.nullcontext=lambda: contextlib.suppress(AssertionError)\n",
            "    try:\n        helper()\n    except Exception:\n        pass\n",
            False,
        ),
    ],
)
def test_source_helper_and_handler_proof(setup, prefix, live):
    source = "import contextlib\n" + setup + "def outer(x):\n" + prefix + TAIL
    namespace = {}
    exec(source, namespace)  # noqa: S102
    original = contextlib.nullcontext
    try:
        if "lambda" in setup:
            assert namespace["outer"](1) is None
        else:
            with pytest.raises(AssertionError):
                namespace["outer"](1)
    finally:
        contextlib.nullcontext = original
    assert verdict(source) is live


@pytest.mark.parametrize("mode", ["raise", "mutate"])
def test_unknown_import_initializer_is_not_inert(monkeypatch, mode):
    source = (
        "import contextlib\ndef outer(x):\n    try:\n        import sentinel_unknown_initializer\n    except ImportError:\n        pass\n"
        + TAIL
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    original = builtins.__import__
    original_null = contextlib.nullcontext

    def controlled(name, *args, **kwargs):
        if name == "sentinel_unknown_initializer":
            if mode == "raise":
                raise ValueError("initializer")
            contextlib.nullcontext = lambda: contextlib.suppress(AssertionError)
            return contextlib
        return original(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", controlled)
        if mode == "raise":
            with pytest.raises(ValueError):
                namespace["outer"](1)
        else:
            assert namespace["outer"](1) is None
    finally:
        contextlib.nullcontext = original_null
        monkeypatch.setattr(builtins, "__import__", original)
    assert verdict(source) is False


@pytest.mark.parametrize("restore", [False, True])
def test_shadow_import_and_later_canonical_restore(restore):
    prefix = "    try:\n        import missing_sentinel_506 as contextlib\n    except ImportError:\n        pass\n"
    if restore:
        prefix += "    import contextlib\n"
    source = "import contextlib\ndef outer(x):\n" + prefix + TAIL
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError if restore else UnboundLocalError):
        namespace["outer"](1)
    assert verdict(source) is restore


def test_source_helper_is_not_a_caller_parameter():
    source = (
        "import contextlib\ndef helper(): return None\ndef outer(x,helper):\n    try:\n        helper()\n    except Exception:\n        pass\n"
        + TAIL
    )
    namespace = {}
    exec(source, namespace)  # noqa: S102
    with pytest.raises(AssertionError):
        namespace["outer"](1, lambda: None)
    assert verdict(source) is False


@pytest.mark.parametrize(
    "setup",
    [
        "def helper(): return None\nhelper=lambda: contextlib.suppress(AssertionError)\n",
        "def decorate(fn): return fn\n@decorate\ndef helper(): return None\n",
        "def helper(v=missing()): return None\n",
        "def helper(): return None\ncontextlib.nullcontext=lambda:contextlib.suppress(AssertionError)\n",
    ],
)
def test_module_helper_setup_effects_decline(setup):
    source = (
        "import contextlib\n"
        + setup
        + "def outer(x):\n    try:\n        helper()\n    except Exception:\n        pass\n"
        + TAIL
    )
    assert verdict(source) is False
