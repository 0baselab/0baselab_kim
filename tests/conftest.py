import pytest

from collector.secrets import clear_sensitive


@pytest.fixture(autouse=True)
def _clean_sensitive():
    clear_sensitive()
    yield
    clear_sensitive()
