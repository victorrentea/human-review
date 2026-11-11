from aws_lambda_powertools import Logger

LOG = Logger()


class Booking:
    def run(self, owner: str, pet_id: int):
        LOG.info("Booking visit for owner %s pet %s", owner, pet_id)
        LOG.debug("cache miss")
        if LOG.isEnabledFor(10):
            LOG.debug("payload=%s", owner)
        LOG.error("boom", exc_info=True)
