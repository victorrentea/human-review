package fx;

public class SpringChild extends SpringBase {
    void create(String beanName) {
        logger.debug("Creating instance of bean '" + beanName + "'");
    }
}
