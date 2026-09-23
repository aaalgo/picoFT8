from __future__ import annotations

import enum

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


NS_PER_SECOND = 1_000_000_000
SLOT_NS = 15 * NS_PER_SECOND


class Base(DeclarativeBase):
    pass


class Direction(str, enum.Enum):
    RX = "RX"
    TX = "TX"


class MessageType(str, enum.Enum):
    CQ = "CQ"

    # <receiver> <sender> <grid>
    GRID = "GRID"

    # <receiver> <sender> -08
    REPORT = "REPORT"

    # <receiver> <sender> R-08
    R_REPORT = "R_REPORT"

    RRR = "RRR"
    RR73 = "RR73"
    MSG_73 = "73"

    UNKNOWN = "UNKNOWN"

class QSOPhase(enum.IntEnum):
    CQ = 0
    REPLIED = 1
    CONFIRMED = 2
    COMPLETE = 3


class Site(Base):
    __tablename__ = "site"

    site_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )


class QSO(Base):
    __tablename__ = "qso"

    qso_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    phase: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=QSOPhase.REPLIED,
        index=True,
    )

    callsign: Mapped[str] = mapped_column(
        String,
        nullable=False,
        index=True,
    )

    grid: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    signal: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )


class FT8Message(Base):
    __tablename__ = "ft8_message"

    message_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    site_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("site.site_id"),
        nullable=True,
        index=True,
    )

    qso_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("qso.qso_id"),
        nullable=True,
        index=True,
    )

    # Unix UTC nanoseconds identifying the message's transmission slot,
    # not the decoder callback time. Preserve subsecond precision on ingestion.
    utc_ns: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=False,
    )

    direction: Mapped[Direction] = mapped_column(
        Enum(
            Direction,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        index=False,
    )

    # Original human-readable FT8 message, e.g.
    #
    #     CQ AC8SS EN82
    #     AC8SS W1ABC FN31
    #     W1ABC AC8SS -08
    #     AC8SS W1ABC R-12
    #     W1ABC AC8SS RR73
    #
    # This is the canonical message content in our application.
    text: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )

    type: Mapped[MessageType] = mapped_column(
        Enum(
            MessageType,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    # FT8 standard messages normally display:
    #
    #     <receiver> <sender> <payload>
    #
    # These fields are parsed from text and stored because the protocol
    # engine uses them constantly.
    sender: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )

    receiver: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        index=True,
    )

    # Decoder-measured SNR.
    #
    # Used for RX messages so that, after:
    #
    #     RX: AC8SS W1ABC FN31   snr_db=-8
    #
    # we can generate:
    #
    #     TX: W1ABC AC8SS -08
    #
    # Normally NULL for TX messages.
    snr_db: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    @property
    def is_odd(self) -> bool:
        """Whether this message belongs to an odd 15-second slot."""
        return bool((self.utc_ns // SLOT_NS) % 2)

    def __repr__(self) -> str:
        return (
            f"FT8Message("
            f"id={self.message_id}, "
            f"utc_ns={self.utc_ns}, "
            f"direction={self.direction.value!r}, "
            f"type={self.type.value!r}, "
            f"sender={self.sender!r}, "
            f"receiver={self.receiver!r}, "
            f"text={self.text!r}"
            f")"
        )
