package fx;
import java.time.Duration;
class DurationLog {
  interface Timer { Timer log(); }
  void go(Timer t, Duration d) { t.log(); }
}
