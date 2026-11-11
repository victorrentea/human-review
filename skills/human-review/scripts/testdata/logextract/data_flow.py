from aws_lambda_powertools import Logger

logger = Logger()


def handle(event, customer_id: str):
    body = event["body"]
    total = len(body)
    for item in body:
        logger.debug("item %s", item)
    logger.info("handled %s for %s", total, customer_id)
