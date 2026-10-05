from __future__ import annotations


def _process_cpu_seconds() -> float:
    """Return reaped child CPU seconds, or 0.0 when unavailable."""

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (ImportError, OSError, ValueError):
        return 0.0
    return float(usage.ru_utime + usage.ru_stime)
