from __future__ import annotations

import io
import logging
import logging.config

import pytest
from uvicorn.config import LOGGING_CONFIG

from app.core.logging_config import APP_LOGGER, configure_app_logging, log_elapsed


@pytest.fixture
def app_stdout():
    # pytest re-binds sys.stdout per phase, so own the stream explicitly.
    logger = logging.getLogger(APP_LOGGER)
    saved = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
    logger.handlers = []
    yield io.StringIO()
    logger.handlers, logger.level, logger.propagate, logger.disabled = saved


def test_an_app_info_record_survives_uvicorns_logging_config(app_stdout):
    # Uvicorn leaves root at WARNING with no handler, so this drops without us.
    logging.config.dictConfig(LOGGING_CONFIG)
    configure_app_logging(stream=app_stdout)

    logging.getLogger("app.web.review_packet.packet").info("pages took 1.27s")

    assert "pages took 1.27s" in app_stdout.getvalue()


def test_configuring_twice_does_not_double_the_output(app_stdout):
    configure_app_logging(stream=app_stdout)
    configure_app_logging(stream=app_stdout)

    logging.getLogger("app.thing").info("once")

    assert app_stdout.getvalue().count("once") == 1


def test_a_phase_that_raises_still_reports_its_elapsed_time(app_stdout):
    configure_app_logging(stream=app_stdout)

    with pytest.raises(ValueError):
        with log_elapsed(logging.getLogger("app.thing"), "slow phase"):
            raise ValueError("boom")

    assert "slow phase took" in app_stdout.getvalue()


def test_a_record_below_a_non_app_logger_is_left_alone(app_stdout):
    # Scoped to `app`, so a noisy dependency's INFO stays off the console.
    configure_app_logging(stream=app_stdout)

    logging.getLogger("httpx").info("chatty")

    assert "chatty" not in app_stdout.getvalue()
