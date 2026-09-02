package fx;
import java.util.logging.Logger;
import java.util.logging.Level;

public class JulExplicit {
    private static final Logger LOGGER = Logger.getLogger(JulExplicit.class.getName());
    void go() {
        LOGGER.info("jul info");
        LOGGER.severe("jul severe");
        LOGGER.log(Level.WARNING, "jul log {0}", 42);
    }
}
