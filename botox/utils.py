import sys


def puts(text, end="\n", flush=True, stream=sys.stdout):
    """
    Print ``text`` to ``stream`` (default: ``sys.stdout``) and auto-flush.

    This is useful for fast loops where Python's default IO buffering would
    prevent "realtime" updating.

    Newlines may be disabled by setting ``end`` to the empty string (``''``).
    (This intentionally mirrors Python 3's ``print`` syntax.)

    You may disable output flushing by setting ``flush=False``.
    """
    stream.write(str(text) + end)
    if flush:
        stream.flush()
