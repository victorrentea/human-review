package fx;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
public class PrivateBase {
    private final Logger secret = LoggerFactory.getLogger(PrivateBase.class);
    void a() { secret.info("mine"); }
}
