#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from uuid import uuid4

from flask import Flask, jsonify, request
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

import ft8
from models import Base, Direction, FT8Message, NS_PER_SECOND
from qso import QSOStatus, RuntimeQSO, TxOffer


def parse_record(record: dict) -> FT8Message:
    if not isinstance(record, dict):
        raise ValueError("Each record must be an object")

    site_id = record["site_id"]
    calibrated_utc_ns = record["calibrated_utc_ns"]
    if type(site_id) is not int or type(calibrated_utc_ns) is not int:
        raise ValueError("site_id and calibrated_utc_ns must be integers")

    decode = record["decode"]
    if not isinstance(decode, dict):
        raise ValueError("decode must be an object")
    text = decode["message"]
    if not isinstance(text, str):
        raise ValueError("decode.message must be a string")
    parts = text.split(" ")[:2]
    if len(parts) != 2 or not all(parts):
        raise ValueError("decode.message must contain receiver and sender")
    receiver, sender = parts

    snr_db = decode.get("snr_db")
    if snr_db is not None and type(snr_db) is not int:
        raise ValueError("decode.snr_db must be an integer or null")

    return FT8Message(
        site_id=site_id,
        qso_id=None,
        utc_ns=calibrated_utc_ns,
        direction=Direction.RX,
        text=text,
        type=ft8.classify_message(text),
        sender=sender,
        receiver=None if receiver == "CQ" else receiver,
        snr_db=snr_db,
    )

MY_CALLSIGN = 'AC8SS'

CALLSIGN2QSO: dict[str, RuntimeQSO] = {}
RUNTIME_LOCK = RLock()
OFFER_RETENTION_NS = 600 * NS_PER_SECOND


@dataclass
class PendingTransmission:
    qso: RuntimeQSO
    offer: TxOffer
    recorded: bool = False


def process_message(message: FT8Message) -> None:
    if message.direction != Direction.RX or message.receiver != MY_CALLSIGN:
        return
    if not message.sender:
        return

    qso = CALLSIGN2QSO.get(message.sender)
    if qso is None:
        qso = RuntimeQSO(message.sender, local_call=MY_CALLSIGN)
        CALLSIGN2QSO[message.sender] = qso

    qso.update_rx(message)


def replay_messages(engine, *, seconds: int, now_utc_ns: int) -> None:
    """Rebuild runtime state from recent inbound messages without rewriting history.

    TX receipts/offers are not persisted, so retry counters restart from the
    latest RX and outstanding handles cannot survive a restart.
    """
    with RUNTIME_LOCK:
        CALLSIGN2QSO.clear()
        if seconds == 0:
            return
        statement = (
            select(FT8Message)
            .where(
                FT8Message.utc_ns >= now_utc_ns - seconds * NS_PER_SECOND,
                FT8Message.utc_ns <= now_utc_ns,
                FT8Message.direction == Direction.RX,
                FT8Message.receiver == MY_CALLSIGN,
            )
            .order_by(FT8Message.utc_ns, FT8Message.message_id)
        )
        with Session(engine) as session:
            for message in session.scalars(statement):
                # Old databases may classify RR73 as GRID. Reparse only the
                # detached object so replay cannot change historical records.
                session.expunge(message)
                message.type = ft8.classify_message(message.text)
                process_message(message)


def create_app(
    database_url: str | None = None, *, now_ns=time.time_ns, replay: int = 1800,
) -> Flask:
    if type(replay) is not int or replay < 0:
        raise ValueError("replay must be a nonnegative number of seconds")
    app = Flask(__name__)
    engine = create_engine(
        database_url or os.environ.get("DATABASE_URL", "sqlite:///db.sqlite3")
    )
    inspector = inspect(engine)
    if inspector.has_table("ft8_message"):
        columns = {column["name"] for column in inspector.get_columns("ft8_message")}
        if "utc" in columns and "utc_ns" not in columns:
            raise RuntimeError(
                "Legacy seconds-based database: migrate ft8_message.utc to utc_ns "
                "as described in API.md before starting the server."
            )
    Base.metadata.create_all(engine)
    replay_messages(engine, seconds=replay, now_utc_ns=now_ns())
    pending_tx: dict[str, PendingTransmission] = {}
    offer_keys: dict[tuple[int, int, int], str] = {}

    def synchronized(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            # RX, offer snapshots, and receipt creation must be atomic relative
            # to one another, including concurrent duplicate acknowledgments.
            with RUNTIME_LOCK:
                now = now_ns()
                for handle, pending in list(pending_tx.items()):
                    offer = pending.offer
                    if now >= offer.target_utc_ns + OFFER_RETENTION_NS:
                        del pending_tx[handle]
                        del offer_keys[(id(pending.qso), offer.revision, offer.target_utc_ns)]
                return function(*args, **kwargs)
        return wrapped

    @app.post("/api/rx/")
    @synchronized
    def receive_messages():
        records = request.get_json()
        if not isinstance(records, list):
            return jsonify(error="Expected a JSON list"), 400

        messages = []
        for index, record in enumerate(records):
            try:
                messages.append(parse_record(record))
            except (KeyError, ValueError) as exc:
                return jsonify(error=f"Record {index}: {exc}"), 400

        with Session(engine) as session, session.begin():
            session.add_all(messages)

            session.flush()    # so that all messages have message_id
            for message in messages:
                process_message(message)
            # automatic commit


        return jsonify(inserted=len(messages)), 201

    @app.get("/api/query/")
    def query_messages():
        try:
            min_message_id = int(request.args.get("min_message_id", "0"))
        except ValueError:
            return jsonify(error="min_message_id must be a nonnegative integer"), 400
        if min_message_id < 0:
            return jsonify(error="min_message_id must be a nonnegative integer"), 400

        statement = (
            select(FT8Message)
            .where(FT8Message.message_id >= min_message_id)
            .order_by(FT8Message.message_id)
        )
        records = []
        with Session(engine) as session:
            for message in session.scalars(statement):
                record = {
                    "message_id": message.message_id,
                    "direction": message.direction.value,
                    "calibrated_utc_ns": message.utc_ns,
                    "decode": {"message": message.text},
                }
                if message.site_id is not None:
                    record["site_id"] = message.site_id
                if message.snr_db is not None:
                    record["decode"]["snr_db"] = message.snr_db
                records.append(record)
        return jsonify(records)

    @app.get("/api/tx/todo/")
    @synchronized
    def transmit_todo():
        target_utc_ns = request.args.get("utc_ns", type=int)
        if target_utc_ns is None:
            return jsonify(error="utc_ns must be an integer"), 400

        if now_ns() >= target_utc_ns + OFFER_RETENTION_NS:
            return jsonify(error="Target slot is outside the acknowledgment window"), 400

        target_is_odd = FT8Message(utc_ns=target_utc_ns).is_odd
        earliest = None
        for qso in list(CALLSIGN2QSO.values()):
            if (
                qso.status != QSOStatus.ACTIVE
                or qso.is_odd is None
                or qso.is_odd == target_is_odd
            ):
                continue
            message, sending_time = qso.request_tx()
            if message is None or sending_time is None:
                continue
            if earliest is None or sending_time < earliest[0]:
                earliest = (sending_time, message, qso)

        if earliest is None or earliest[0] > target_utc_ns:
            return jsonify(message="CQ AC8SS EN82", handle=None)

        sending_time, message, qso = earliest
        key = (id(qso), qso.scheduling_revision, target_utc_ns)
        handle = offer_keys.get(key)
        if handle is None:
            handle = uuid4().hex
            offer = TxOffer(
                message=message,
                target_utc_ns=target_utc_ns,
                earliest_utc_ns=sending_time,
                required_parity=int(target_is_odd),
                phase=qso.phase,
                revision=qso.scheduling_revision,
            )
            pending_tx[handle] = PendingTransmission(qso, offer)
            offer_keys[key] = handle
        return jsonify(message=message, handle=handle)

    @app.post("/api/tx/")
    @synchronized
    def record_transmission():
        record = request.get_json()
        if not isinstance(record, dict):
            return jsonify(error="Expected a JSON object"), 400
        sent_utc_ns = record.get("utc_ns")
        if type(sent_utc_ns) is not int:
            return jsonify(error="utc_ns must be an integer"), 400
        if "handle" not in record:
            return jsonify(error="handle is required"), 400
        handle = record["handle"]
        if handle is None:
            # CQ transmissions do not belong to a QSO.
            return jsonify(recorded=True)
        if not isinstance(handle, str):
            return jsonify(error="handle must be a string or null"), 400

        pending = pending_tx.get(handle)
        if pending is None:
            return jsonify(error="Unknown or expired TX handle"), 409
        if sent_utc_ns != pending.offer.target_utc_ns:
            return jsonify(error="Transmission must use the offered target slot"), 409
        if not pending.recorded:
            try:
                pending.qso.record_tx(sent_utc_ns, pending.offer)
            except ValueError as exc:
                return jsonify(error=str(exc)), 409
            pending.recorded = True
        return jsonify(recorded=True)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument(
        "--replay", type=int, default=1800, metavar="SECONDS",
        help="replay recent database messages at startup (default: 1800; 0 disables)",
    )
    args = parser.parse_args()
    if args.replay < 0:
        parser.error("--replay must be nonnegative")
    create_app(replay=args.replay).run(host="0.0.0.0", port=args.port)
