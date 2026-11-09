"""Sending a gate-checked file to the Longevity Mothership relay and the local log of what was sent.
Contributor files (contributions, withdrawals, registry updates) go with the instance's install token and are logged in
<instance>/mothership/sent.jsonl; professional files (preregistrations, findings) go with the professional token and are
logged in <PLSI_HOME>/mothership/sent.jsonl. The relay's reply only says the Mother was woken; its `ticket` fetches
the Mother's own receipt later (`receipt`, into acks.jsonl next to sent.jsonl). Research notes never go to the relay:
`plsi mothership email`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from plsi import files
from plsi.clock import now
from plsi.errors import NetworkError, NotFound, UsageError, ValidationError
from plsi.mothership import gate, professional, scan
from plsi.mothership.enroll import folder, forget_install, install, pending, sends
from plsi.mothership.relay import address, post, refused
from plsi.vocab import MothershipMessage, ProfessionalScope, ReceiptState, WithdrawalRequest


def _sender(doc: Any, instance: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """(instance, professional token payload): exactly one of them names the sender of `doc`."""
    kind = doc.get("kind") if isinstance(doc, dict) else None
    if kind == MothershipMessage.RESEARCH_NOTE:
        raise UsageError("research notes go to the Mothership by email, signed by the sender's address",
                         code="EMAIL_ONLY", hint="plsi mothership email --file <file>")
    if kind == MothershipMessage.FEEDBACK:
        raise UsageError("feedback goes to the Mothership by email", code="EMAIL_ONLY", hint="plsi feedback report --file <draft>")
    if kind in gate.PROFESSIONAL and instance:
        raise UsageError(f"a {kind} is a professional message; send it without --instance")
    if instance or kind not in gate.PROFESSIONAL:
        return instance, None
    return None, professional.payload(ProfessionalScope.FINDINGS_SUBMIT)


def _gated(file: str, instance: str | None, expected: str | None = None) -> tuple[Any, str | None, dict[str, Any] | None]:
    """The file and its sender (instance or professional token payload), once the file passed the gate."""
    doc = scan.load(file)
    gate.approved(doc, expected)
    instance, token = _sender(doc, instance)
    gate.require(doc, instance, pid=token["pid"] if token else None)
    return doc, instance, token


def check(file: str, instance: str | None = None, expected: str | None = None) -> dict[str, Any]:
    """`mothership check`: run the gate on a file without sending or logging it.

    A contribution is also rebuilt from its `minimization` record. A hand edit fails even when the
    gate would still accept the shape."""
    doc, instance, _ = _gated(file, instance, expected)
    if instance and isinstance(doc, dict) and doc.get("kind") == MothershipMessage.CONTRIBUTION and isinstance(doc.get("minimization"), dict):
        from plsi.mothership.pack import document

        fresh = document(instance, doc["minimization"])
        if files.hash_json(fresh) != files.hash_json(doc):
            raise ValidationError("the package does not match mothership pack for its minimization",
                                  code="PACKAGE_MISMATCH",
                                  hint="regenerate it with plsi mothership pack; do not edit the file")
    return {"file": file, "ok": True, "to": address(), "package_hash": files.hash_json(doc)}


def send(file: str, expected: str, instance: str | None = None) -> dict[str, Any]:
    """`mothership send`: gate-check a file, POST it to the relay with the sender's token and log it with the relay's
    reply and ticket. Not logged when the relay refuses; never retried. Delivery is not storage: the Mother's receipt
    comes from `receipt`. A delete_all file keeps its proof until that receipt says deleted. Only the approved file
    (`expected`, its package_hash) goes."""
    doc, instance, _ = _gated(file, instance, expected)
    if instance:
        found = install(instance)
        if found is None:
            raise NotFound(f"{instance!r} has no relay install token", code="NOT_ENROLLED",
                           hint=f"plsi mothership join --instance {instance} --code <code from {address()}/join> ...")
        _not_again(instance, doc)
        bearer, log = found["token"], folder(instance) / "sent.jsonl"
    else:
        bearer, log = professional.token(), professional.folder() / "sent.jsonl"
    try:
        reply = post("/relay", doc, bearer)
    except NetworkError as exc:
        raise refused(exc, "file") from None
    entry = {"sent_at": now().isoformat(), "file": file, "kind": doc["kind"], "to": address(),
             "package_hash": files.hash_json(doc), "kinds": doc.get("kinds", []), "request": doc.get("request"),
             "id": doc.get("preregistration_id") if doc["kind"] == MothershipMessage.PREREGISTRATION else doc.get("finding_id"),
             "ticket": reply.get("ticket") if isinstance(reply, dict) else None,
             "reply": reply if isinstance(reply, dict) else {"text": str(reply)[:500]}}
    entry = {k: v for k, v in entry.items() if v not in (None, [])}
    files.append_jsonl(log, entry)
    return entry


def _not_again(instance: str, doc: dict[str, Any]) -> None:
    """The same contribution twice: refused when the Mother already stored it or its receipt is pending; one that was
    refused, never confirmed or sent before receipts may go again."""
    if doc.get("kind") != MothershipMessage.CONTRIBUTION:
        return
    digest = files.hash_json(doc)
    for e in sends(instance):
        if e.get("package_hash") != digest:
            continue
        if e["receipt_state"] == ReceiptState.STORED:
            raise UsageError("the Mothership already stored this package", code="ALREADY_STORED",
                             hint=f"receipt {e.get('receipt')}; nothing new to send")
        if e["receipt_state"] == ReceiptState.PENDING:
            raise UsageError("this package was sent and its receipt is pending", code="ALREADY_SENT",
                             hint=f"plsi mothership receipt --instance {instance} --wait 120")


POLL_S = 15


def receipt(instance: str | None = None, wait: int = 0) -> dict[str, Any]:
    """`mothership receipt`: ask the relay for the Mother's receipt of every sent message that has none yet, keep each
    answer in acks.jsonl and list what is done, refused and still pending. With `wait` (seconds, at most 300) it asks
    again every 15 s while anything is pending. A delete_all whose receipt says deleted drops the proof: from the
    withdrawal file and the install itself."""
    deadline = time.monotonic() + max(0, min(wait, 300))
    out = _receipt_once(instance)
    while out["pending"] and time.monotonic() + POLL_S <= deadline:
        time.sleep(POLL_S)
        more = _receipt_once(instance)
        out = {"done": out["done"] + more["done"], "refused": out["refused"] + more["refused"], "pending": more["pending"]}
    return out


def _receipt_once(instance: str | None) -> dict[str, Any]:
    if instance:
        found = install(instance)
        if found is None:
            raise NotFound(f"{instance!r} has no relay install token", code="NOT_ENROLLED")
        bearer, base, waiting = found["token"], folder(instance), pending(instance)
    else:
        bearer, base = professional.token(), professional.folder()
        done = {a.get("ticket") for a in files.read_jsonl(base / "acks.jsonl")}
        waiting = [e for e in files.read_jsonl(base / "sent.jsonl") if e.get("ticket") and e["ticket"] not in done]
    out: dict[str, list[dict[str, Any]]] = {"done": [], "refused": [], "pending": []}
    for entry in waiting:
        try:
            reply = post("/ack", {"ticket": entry["ticket"]}, bearer)
        except NetworkError as exc:
            raise refused(exc, "receipt request") from None
        line = {"kind": entry["kind"], "sent_at": entry["sent_at"], "ticket": entry["ticket"]}
        if not isinstance(reply, dict) or reply.get("status") != "done":
            out["pending"].append(line)
            continue
        ack = reply.get("ack") if isinstance(reply.get("ack"), dict) else {}
        files.append_jsonl(base / "acks.jsonl", line | {"at": now().isoformat(), "ack": ack})
        out["done" if ack.get("ok") else "refused"].append(line | {"ack": ack})
        deleted = ack.get("ok") and ack.get("status") == "deleted"
        if instance and deleted and entry.get("request") == WithdrawalRequest.DELETE_ALL:
            _drop_proof(instance, entry.get("file"))
            break  # the relay retires this install once it reads the deletion: nothing more to ask with its token
    return out


def _drop_proof(instance: str, file: str | None) -> None:
    """The deletion is confirmed: the contributor secret is no longer needed anywhere."""
    if file and file != "-" and Path(file).is_file():
        doc = files.load_json(file)
        files.atomic_write_json(Path(file), {k: v for k, v in doc.items() if k != "proof"})
    forget_install(instance)
