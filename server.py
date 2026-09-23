#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

from flask import Flask, jsonify, request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import ft8
from models import Base, Direction, FT8Message


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
        utc=calibrated_utc_ns // 1_000_000_000,
        direction=Direction.RX,
        text=text,
        type=ft8.classify_message(text),
        sender=sender,
        receiver=None if receiver == "CQ" else receiver,
        snr_db=snr_db,
    )


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    engine = create_engine(
        database_url or os.environ.get("DATABASE_URL", "sqlite:///db.sqlite3")
    )
    Base.metadata.create_all(engine)

    @app.post("/api/rx/")
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

        return jsonify(inserted=len(messages)), 201

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7777)
    args = parser.parse_args()
    create_app().run(host="0.0.0.0", port=args.port)
