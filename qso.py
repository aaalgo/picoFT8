from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from models import FT8Message, MessageType, NS_PER_SECOND, QSOPhase, SLOT_NS


# Retry durations are seconds; all UTC timestamps are nanoseconds.
RETRY_DELAYS = [30] * 5 + [60] * 10 + [120] * 10

MESSAGE_PHASES = {
    MessageType.CQ: QSOPhase.CQ,
    MessageType.GRID: QSOPhase.REPLIED,
    MessageType.REPORT: QSOPhase.CONFIRMED,
    MessageType.R_REPORT: QSOPhase.CONFIRMED,
    MessageType.RRR: QSOPhase.CONFIRMED,
    MessageType.RR73: QSOPhase.COMPLETE,
    MessageType.MSG_73: QSOPhase.COMPLETE,
}


class QSOStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


@dataclass
class QSOStage:
    phase: QSOPhase

    response_text: Optional[str] = None

    rx_count: int = 0
    mismatched_rx_count: int = 0
    tx_count: int = 0

    entered_utc_ns: Optional[int] = None
    last_rx_utc_ns: Optional[int] = None
    last_tx_utc_ns: Optional[int] = None

    last_rx_message_id: Optional[int] = None


@dataclass(frozen=True)
class TxOffer:
    """Immutable context for a particular transmission slot."""

    message: str
    target_utc_ns: int
    earliest_utc_ns: int
    required_parity: int
    phase: QSOPhase
    revision: int


@dataclass
class RuntimeQSO:
    callsign: str
    # The cursor counts transmissions since the latest RX. There are at most
    # len(RETRY_DELAYS) transmissions per reset, with no final response window.
    retry_index: int = field(default=0, init=False)
    next_tx_utc_ns: Optional[int] = field(default=None, init=False)
    scheduling_revision: int = field(default=0, init=False)
    is_odd: Optional[bool] = field(default=None, init=False)  # Latest RX slot parity.
    qso_id: Optional[int] = None
    grid: Optional[str] = None
    signal: Optional[int] = None

    local_call: str = "AC8SS"

    stages: list[QSOStage] = field(
        default_factory=lambda: [QSOStage(phase) for phase in QSOPhase]
    )

    # Phases only advance: CQ -> REPLIED -> CONFIRMED -> COMPLETE;
    # skipped phases are allowed. Older messages never downgrade the phase.
    # COMPLETE can remain ACTIVE while the final 73 awaits transmission.
    # Status transitions: ACTIVE <-> EXPIRED, ACTIVE -> COMPLETED.
    # RX revives EXPIRED before advancing phases. COMPLETED is terminal:
    # further RX updates statistics/parity only, without scheduling more TX.
    phase: QSOPhase = QSOPhase.CQ
    status: QSOStatus = QSOStatus.ACTIVE

    @property
    def current_stage(self) -> QSOStage:
        return self.stages[self.phase]

    def _activate_stage(
        self,
        phase: QSOPhase,
        *,
        utc_ns: int,
        response_text: Optional[str],
    ) -> None:
        assert phase > self.phase, "QSO phases must advance strictly"
        self.phase = phase
        stage = self.current_stage

        if stage.entered_utc_ns is None:
            stage.entered_utc_ns = utc_ns

        stage.response_text = response_text

        if phase == QSOPhase.COMPLETE and response_text is None:
            self.status = QSOStatus.COMPLETED
            self.next_tx_utc_ns = None

    def _report_response(self) -> Optional[str]:
        if self.signal is None:
            return None

        return (
            f"{self.callsign} "
            f"{self.local_call} "
            f"{self.signal:+03d}"
        )

    def _rr73_response(self) -> str:
        return f"{self.callsign} {self.local_call} RR73"

    def update_rx(self, msg: FT8Message) -> None:
        """
        Consume an incoming message belonging to this QSO.

        Responsibilities:
        - update RX/liveness information
        - revive an expired QSO
        - advance protocol phase when appropriate
        - construct the response for the current phase
        - reset retry scheduling without invalidating issued offers
        - retain terminal completion while recording further RX
        """

        if msg.sender != self.callsign:
            raise ValueError(
                f"RX sender {msg.sender!r} does not match "
                f"QSO callsign {self.callsign!r}"
            )

        self.scheduling_revision += 1

        # RX revives an expired QSO, but cannot revive a completed QSO.
        if self.status == QSOStatus.EXPIRED:
            self.status = QSOStatus.ACTIVE
        self.is_odd = msg.is_odd

        incoming_phase = MESSAGE_PHASES.get(msg.type)
        if incoming_phase is not None and incoming_phase > self.phase:
            if msg.snr_db is not None:
                self.signal = msg.snr_db
            responses = {
                QSOPhase.REPLIED: self._report_response,
                QSOPhase.CONFIRMED: self._rr73_response,
            }
            response = responses.get(incoming_phase)
            self._activate_stage(
                incoming_phase,
                utc_ns=msg.utc_ns,
                response_text=(
                    f"{self.callsign} {self.local_call} 73"
                    if msg.type == MessageType.RR73
                    else response() if response is not None else None
                ),
            )

        if msg.type == MessageType.MSG_73:
            self.status = QSOStatus.COMPLETED
            self.next_tx_utc_ns = None

        stage = self.current_stage
        if incoming_phase == self.phase:
            stage.rx_count += 1
        else:
            # Older or unrecognized messages prove liveness without
            # counting as a response for the current phase.
            stage.mismatched_rx_count += 1
        stage.last_rx_utc_ns = msg.utc_ns
        stage.last_rx_message_id = msg.message_id
        if self.status == QSOStatus.ACTIVE:
            self.retry_index = 0
            # The next 15-second slot has the opposite parity to this RX.
            self.next_tx_utc_ns = (msg.utc_ns // SLOT_NS + 1) * SLOT_NS

    def request_tx(self) -> tuple[Optional[str], Optional[int]]:
        """Return the current response and earliest sending time, without issuing it."""
        if self.status == QSOStatus.ACTIVE and self.retry_index >= len(RETRY_DELAYS):
            self.expire()
        if (
            self.status != QSOStatus.ACTIVE
            or self.current_stage.response_text is None
            or self.next_tx_utc_ns is None
        ):
            return None, None
        return self.current_stage.response_text, self.next_tx_utc_ns

    def record_tx(self, sent_utc_ns: int, offer: TxOffer) -> None:
        """Account for an issued offer; the server deduplicates acknowledgments.

        Validate the snapshot, never the current RX parity or phase. Only an
        offer from the current revision may change the current retry schedule.
        """
        if sent_utc_ns != offer.target_utc_ns:
            raise ValueError("Transmission must use the offered target slot")
        if sent_utc_ns < offer.earliest_utc_ns:
            raise ValueError("Transmission precedes its eligible sending time")
        if (sent_utc_ns // SLOT_NS) % 2 != offer.required_parity:
            raise ValueError("Transmission must use the offered parity")

        stage = self.stages[offer.phase]
        stage.tx_count += 1
        # Acknowledgments may arrive out of transmission order.
        if stage.last_tx_utc_ns is None or sent_utc_ns > stage.last_tx_utc_ns:
            stage.last_tx_utc_ns = sent_utc_ns
        # A final 73 needs no reply or retries once transmission is confirmed.
        # Even a receipt delayed across another RX proves it was sent.
        if offer.phase == QSOPhase.COMPLETE and self.status != QSOStatus.COMPLETED:
            self.status = QSOStatus.COMPLETED
            self.next_tx_utc_ns = None
            self.scheduling_revision += 1
        if (
            offer.revision != self.scheduling_revision
            or self.status != QSOStatus.ACTIVE
        ):
            return
        delay = RETRY_DELAYS[self.retry_index]
        self.retry_index += 1
        self.scheduling_revision += 1
        if self.retry_index == len(RETRY_DELAYS):
            self.expire()
        else:
            self.next_tx_utc_ns = sent_utc_ns + delay * NS_PER_SECOND

    def expire(self) -> None:
        """Stop scheduling; issued offers remain recordable and completion terminal."""
        self.scheduling_revision += 1
        self.next_tx_utc_ns = None
        if self.status != QSOStatus.COMPLETED:
            self.status = QSOStatus.EXPIRED
