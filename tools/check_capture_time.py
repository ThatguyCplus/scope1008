"""How long does a burst capture take to become ready at each timebase?

Polls the a55a 'ready' command with a generous budget and reports the wait. The ratio
(capture time / timebase) gives the real number of divisions per record, which pins down
SAMPLES_PER_DIV.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402
from scope1008.hantek1008 import Hantek1008Raw  # noqa: E402

wait = {"t": 0.0, "polls": 0}
orig = Hantek1008Raw._Hantek1008Raw__send_a55a_command


def timed(self, attempts=20):
    t = time.perf_counter()
    for i in range(100000):
        resp = self._Hantek1008Raw__send_cmd(0xa5, parameter=[0x5a], response_length=1)
        if resp[0] in (2, 3):
            wait["t"] = time.perf_counter() - t
            wait["polls"] = i + 1
            return
        if time.perf_counter() - t > 15:
            break
        time.sleep(0.005)
        self._Hantek1008Raw__send_ping()
    raise RuntimeError("a55a never ready within 15 s")


Hantek1008Raw._Hantek1008Raw__send_a55a_command = timed

b = B.DeviceBackend()
b.open()
for ns in (500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000,
           100_000_000, 200_000_000):
    b.configure(B.AcqConfig(active=[1], ns_per_div=ns, trig_ch=1))
    res = []
    for _ in range(2):
        fr = b.acquire_burst()
        res.append((wait["t"], wait["polls"], min(len(v) for v in fr.data.values())))
    t, polls, n = res[-1]
    print(f"{ns / 1e6:7.1f} ms/div: ready after {t * 1e3:7.0f} ms ({polls} polls), {n} samples "
          f"-> {t / (ns * 1e-9):6.1f} div of capture, {n / max(t / (ns * 1e-9), 1e-9):6.0f} samples/div")
b.close()
