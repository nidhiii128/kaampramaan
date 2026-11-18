import threading

from app import ledger


def _add(db, wid, day, amount=700, kind="work", **kw):
    return ledger.append(db, ledger.NewEntry(worker_id=wid, kind=kind, work_date=day,
                                             amount=amount, source="test", **kw))


def test_chain_links_entries(db):
    wid = db.create_worker("911")
    a = _add(db, wid, "2026-11-01")
    b = _add(db, wid, "2026-11-02")
    assert a["prev_hash"] == ledger.GENESIS
    assert b["prev_hash"] == a["hash"]
    assert ledger.verify_chain(db, wid).ok


def test_edit_is_detected(db):
    wid = db.create_worker("911")
    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        _add(db, wid, d)
    with db.connect() as c:  # someone quietly lowers a wage
        c.execute("UPDATE entries SET amount = 100 WHERE worker_id = ? AND seq = 2", (wid,))
    check = ledger.verify_chain(db, wid)
    assert not check.ok and check.broken_at_seq == 2


def test_deletion_is_detected(db):
    wid = db.create_worker("911")
    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        _add(db, wid, d)
    with db.connect() as c:
        c.execute("DELETE FROM entries WHERE worker_id = ? AND seq = 2", (wid,))
    assert not ledger.verify_chain(db, wid).ok


def test_replaced_media_is_detected(db, tmp_path):
    wid = db.create_worker("911")
    f = tmp_path / "note.ogg"
    f.write_bytes(b"original voice note")
    _add(db, wid, "2026-11-01", media_path=str(f), media_sha256=ledger.sha256_bytes(f.read_bytes()))
    reader = lambda p: open(p, "rb").read()
    assert ledger.verify_chain(db, wid, media_reader=reader).ok
    f.write_bytes(b"different audio")
    check = ledger.verify_chain(db, wid, media_reader=reader)
    assert not check.ok and "replaced" in check.reason


def test_concurrent_appends_keep_sequence(db):
    wid = db.create_worker("911")
    threads = [threading.Thread(target=_add, args=(db, wid, "2026-11-01")) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in db.list_entries(wid)]
    assert seqs == list(range(1, 21))
    assert ledger.verify_chain(db, wid).ok


def test_rejects_negative_amount(db):
    wid = db.create_worker("911")
    import pytest
    with pytest.raises(ValueError):
        _add(db, wid, "2026-11-01", amount=-5)
