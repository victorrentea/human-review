import sys
import traceback


def go(x):
    print("debugging", x)
    try:
        return 1 / x
    except ZeroDivisionError:
        traceback.print_exc()
        sys.stderr.write("oops\n")
