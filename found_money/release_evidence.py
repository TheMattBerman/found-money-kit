"""Whether committed release-evidence artifacts must bind to the current tree.

Found Money carries a release process: fresh-clone receipts, publication packets,
independent blind reviews of the generated plays, a source/proper-noun audit of
the public tree, and ratified visual goldens. Each one is real evidence and each
one is expensive to refresh. A blind review is two independent model runs per
artifact. The source audit is another. The goldens need a human to look.

That is the right cost to pay at a release. It is the wrong cost to pay on every
commit of a product that has not been released, and the committed publication
packet says `not_ready` precisely because it has not been.

So binding is opt-in. Set ``FM_RELEASE_EVIDENCE=1`` and everything binds exactly
as before. Leave it unset and the evidence artifacts are treated as the historical
records they are, while every safety check still runs: redaction, the public-safety
scan, the no-mutation capability scan, and the deterministic concept-card checks.
None of those are gated, because none of them need a model or a human to refresh.
"""

from __future__ import annotations

import os

RELEASE_EVIDENCE_ENV = "FM_RELEASE_EVIDENCE"


def release_evidence_enabled() -> bool:
    """True when committed evidence must bind to the current tree."""

    return os.environ.get(RELEASE_EVIDENCE_ENV, "").strip() == "1"
