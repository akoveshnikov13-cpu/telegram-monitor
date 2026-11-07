"""Hosting entry point: retry startup failures without interactive input."""
import time
from bot import main


if __name__ == "__main__":
    while True:
        try:
            code = main()
            if code == 0:
                break
            print("Повтор запуска через 30 секунд.", flush=True)
            time.sleep(30)
        except KeyboardInterrupt:
            break
