from __future__ import annotations

import bl_context


def test_version() -> None:
    assert bl_context.__version__ == "0.0.1"
