import logging
import logging as lg

log = logging.getLogger(__name__)
other = lg.getLogger("other")


def handle(event_id):
    log.warning("retrying %s", event_id)
    other.critical("down")
    logging.info("root logger %s", event_id)
    log.log(logging.INFO, "via log() %s", event_id)
    log.exception("failed")
