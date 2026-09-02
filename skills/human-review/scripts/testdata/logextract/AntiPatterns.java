package fx;
public class AntiPatterns {
    void go(Exception e) {
        System.out.println("hello");
        System.err.println("bad");
        e.printStackTrace();
        System.out.printf("%s%n", "x");
    }
}
