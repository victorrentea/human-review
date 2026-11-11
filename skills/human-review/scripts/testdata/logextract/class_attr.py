import logging
from aws_lambda_powertools import Logger


class Service:
    logger = Logger(service="svc")

    def __init__(self):
        self.audit = logging.getLogger("audit")

    def run(self):
        self.logger.info("class attribute")
        self.audit.warning("instance attribute")

    @classmethod
    def make(cls):
        cls.logger.debug("through cls")


def with_param(logger: Logger, n):
    logger.info("annotated parameter %s", n)
