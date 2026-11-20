"""Shrink a failing operation sequence to a minimal explainable segment.

The shrinker is a delta-debugging (ddmin) loop over the operation list: it
repeatedly tries to drop chunks of operations while the failure stays
reproducible. "Reproducible" means a fresh replay of the candidate sequence
still diverges with the same signature (same operation kind, same real-side
outcome kind, same model-side outcome kind) as the original failure, which
keeps the shrinker focused on one bug instead of hopping between unrelated
ones. Operations whose symbolic references dangle after removals (cursor
slots, named pins, generations) are skipped symmetrically by the runner, so
removal-only shrinking is always well-defined. The result is 1-minimal:
removing any single remaining operation makes the failure disappear.

A tiny self-test (`python3 -m verification.shrink`) checks the ddmin loop
itself against a synthetic oracle.
"""

import json

from .runner import run_scenario


def ddmin(ops, interesting):
    """Classic ddmin chunk-removal followed by a 1-minimal cleanup pass."""
    ops = list(ops)
    granularity = 2
    while len(ops) >= 2:
        chunk = (len(ops) + granularity - 1) // granularity
        reduced = False
        index = 0
        while index < len(ops):
            candidate = ops[:index] + ops[index + chunk:]
            if candidate and interesting(candidate):
                ops = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
            index += chunk
        if not reduced:
            if granularity >= len(ops):
                break
            granularity = min(len(ops), granularity * 2)
    changed = True
    while changed:
        changed = False
        for index in range(len(ops)):
            candidate = ops[:index] + ops[index + 1:]
            if candidate and interesting(candidate):
                ops = candidate
                changed = True
                break
    return ops


def shrink_inner(ops, interesting):
    """Remove individual batch operations inside commit ops (1-minimal)."""
    ops = list(ops)
    changed = True
    while changed:
        changed = False
        for index, op in enumerate(ops):
            if op.get("op") != "commit" or len(op.get("ops", [])) <= 1:
                continue
            sub = op["ops"]
            for position in range(len(sub)):
                shrunken = dict(op, ops=sub[:position] + sub[position + 1:])
                candidate = ops[:index] + [shrunken] + ops[index + 1:]
                if interesting(candidate):
                    ops = candidate
                    changed = True
                    break
            if changed:
                break
    return ops


def shrink_failure(ops, original):
    """Shrink `ops` so it still diverges with `original.signature()`.

    Alternates operation-list ddmin with intra-batch shrinking until a
    fixpoint: the result is 1-minimal both across operations and across the
    batch members inside each commit.
    """
    target = original.signature()
    attempts = {"count": 0}

    def interesting(candidate):
        attempts["count"] += 1
        divergence = run_scenario(candidate)
        return divergence is not None and divergence.signature() == target

    minimal = ddmin(ops, interesting)
    while True:
        before = json.dumps(minimal, sort_keys=True)
        minimal = ddmin(shrink_inner(minimal, interesting), interesting)
        if json.dumps(minimal, sort_keys=True) == before:
            break
    return minimal, run_scenario(minimal), attempts["count"]


def render(ops, divergence=None):
    """Human-readable rendering of a (minimal) failing segment."""
    lines = []
    for index, op in enumerate(ops):
        marker = ">>" if divergence is not None and index == divergence.index else "  "
        lines.append("%s %03d %s" % (marker, index,
                                     json.dumps(op, ensure_ascii=False, sort_keys=True)))
    if divergence is not None:
        lines.append("real : " + json.dumps(divergence.real, ensure_ascii=False, sort_keys=True))
        lines.append("model: " + json.dumps(divergence.model, ensure_ascii=False, sort_keys=True))
        if divergence.note:
            lines.append("note : " + divergence.note)
    return "\n".join(lines)


def _self_test():
    # Synthetic oracle: a sequence "fails" when it contains both a 1 and a 2
    # in positions whose values sum to >= 3; the minimal witness is [1, 2].
    def interesting(candidate):
        return 1 in candidate and 2 in candidate
    result = ddmin([0, 1, 0, 2, 0, 3, 1], interesting)
    assert result == [1, 2], result

    calls = {"count": 0}

    def counted(candidate):
        calls["count"] += 1
        return sum(candidate) >= 7 and 5 in candidate
    result = ddmin([5, 5, 5, 1, 1, 1], counted)
    assert result == [5, 5], result  # 1-minimal: dropping either 5 breaks the sum
    assert calls["count"] < 60
    print("shrink self-test ok")


if __name__ == "__main__":
    _self_test()
