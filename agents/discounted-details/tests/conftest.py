from __future__ import annotations

import pytest

from app.db import DB
from app.seed import SERVICES_ROWS, SETTINGS_ROWS
from app.services.sheets import MemoryStore, SheetsCRM


@pytest.fixture
def services():
    return [dict(r) for r in SERVICES_ROWS]


@pytest.fixture
def settings():
    return {k: v for k, v, _ in SETTINGS_ROWS}


@pytest.fixture
def store():
    s = MemoryStore()
    for r in SERVICES_ROWS:
        s.append("Services", r)
    for k, v, d in SETTINGS_ROWS:
        s.append("Settings", {"key": k, "value": v, "description": d})
    return s


@pytest.fixture
def crm(store):
    return SheetsCRM(store)


@pytest.fixture
def db():
    return DB(":memory:")
