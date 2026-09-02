package fx;
public class PrivateChild extends PrivateBase {
    void b(Object secret) { secret.info("not visible - private in the base"); }
}
