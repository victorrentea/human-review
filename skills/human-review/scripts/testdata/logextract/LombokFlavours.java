package fx;
import lombok.extern.log4j.Log4j2;

@Log4j2
class L4j2 { void a() { log.fatal("dead"); } }

@lombok.extern.apachecommons.CommonsLog
class Cmns { void a() { log.info("cl"); } }

@lombok.extern.java.Log
class Jul { void a() { log.warning("jul"); } }

@lombok.extern.flogger.Flogger
class Flg { void a() { logger.atInfo().log("flogger %s", "x"); } }

@lombok.extern.slf4j.XSlf4j
class XS { void a() { log.debug("xs"); } }
