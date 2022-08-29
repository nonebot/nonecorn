from __future__ import annotations

import pytest
import trio

from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.config import Config
from hypercorn.trio.lifespan import Lifespan
from hypercorn.utils import LifespanFailureError, LifespanTimeoutError
from ..helpers import lifespan_failure, SlowLifespanFramework


@pytest.mark.trio
async def test_startup_timeout_error(nursery: trio._core._run.Nursery) -> None:
    config = Config()
    config.startup_timeout = 0.01
    lifespan = Lifespan(ASGIWrapper(SlowLifespanFramework(0.02, trio.sleep)), config)
    nursery.start_soon(lifespan.handle_lifespan)
    with pytest.raises(LifespanTimeoutError) as exc_info:
        await lifespan.wait_for_startup()
    assert str(exc_info.value).startswith("Timeout whilst awaiting startup")


@pytest.mark.trio
async def test_startup_failure() -> None:
    lifespan = Lifespan(ASGIWrapper(lifespan_failure), Config())
    with pytest.raises(LifespanFailureError) as exc_info:
        async with trio.open_nursery() as lifespan_nursery:
            await lifespan_nursery.start(lifespan.handle_lifespan)
            await lifespan.wait_for_startup()

    assert str(exc_info.value) == "Lifespan failure in startup. 'Failure'"
