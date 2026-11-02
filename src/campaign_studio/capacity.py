"""Conservative admission estimates, not filesystem reservations or provider limits."""

# A 1920² RGBA PNG, its retained base64 response, and an atomic temporary write.
# Returned provider bytes are always preserved without a quota check; only the
# next growth-producing operation is refused if actual usage exceeds the budget.
MASTER_RECEIPT_BYTES = 64_000_000
DOCUMENT_BYTES = 1_000_000


def revision_bytes(plan) -> int:
    # PNG can be larger than raw pixels. Include filter rows, chunk/deflate
    # overhead, and the largest file's temporary atomic-write footprint.
    sizes = [int((o.width * o.height * 4 + o.height) * 1.01) + 65_536 for o in plan.outputs]
    return sum(sizes) + max(sizes, default=0) + DOCUMENT_BYTES


def production_bytes(plan, pending_requests: int) -> int:
    return revision_bytes(plan) + pending_requests * MASTER_RECEIPT_BYTES
