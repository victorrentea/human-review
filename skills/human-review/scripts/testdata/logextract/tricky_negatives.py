import math
from aws_lambda_powertools import Logger

logger = Logger()


class History:
    def info(self, x):
        return x


def go(x, record):
    math.log(x)
    History().info("not a logger")
    record.error("an attribute that is not a logger")
    logger.isEnabledFor(10)
    logger.append_keys(order_id=x)
    return x
