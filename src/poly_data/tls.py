from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_CONFIGURED = False


def configure_system_truststore() -> None:
    """Use the OS certificate store for HTTPS clients when truststore is present."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        _CONFIGURED = True
    except Exception as e:  # pragma: no cover - defensive startup path
        logger.debug("system truststore setup skipped: %s", e)
