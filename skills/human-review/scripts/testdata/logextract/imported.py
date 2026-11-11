from shared.log import logger
from ludvig_shared.tools import logger as shared_logger


def go(activity_id):
    logger.info("imported from a scanned module", extra={"activity_id": activity_id})
    shared_logger.info("imported from outside the scan")
