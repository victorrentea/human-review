from aws_lambda_powertools import Logger

logger = Logger()


def go(user, payload, record, org_number, activity_id):
    logger.info("user", extra={"email": user.email, "activity_id": activity_id})
    logger.info("RPA payload", extra={"payload": payload})
    logger.warning(f"no handler for {record.get('body')}")
    logger.info("org", extra={"orgNumber": org_number})
    logger.info("clean", extra={"activity_id": activity_id})
